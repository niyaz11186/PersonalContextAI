"""PostgresCheckpointSaver — LangGraph durable state on our own PostgreSQL.

Layer L2. **The only checkpointer, and one of only two modules permitted to import
`langgraph`** (boundary rule 2).

ADR-006 justifies the LangGraph dependency on exactly one capability: a workflow that
interrupts, survives a process restart, and resumes with intact state. That capability
is this file. Without a durable checkpointer the dependency buys nothing that a plain
async function could not do.

Why not `langgraph-checkpoint-postgres`: it requires `psycopg`, which would put a
second PostgreSQL driver and a second connection pool beside the existing asyncpg one,
for a table layout we would then not control. Instead the work is split across a seam —
this half knows LangGraph and no SQL; `adapters/postgres/checkpoint_repository.py`
knows SQL and no LangGraph.

Semantics below were taken from the installed `langgraph==1.2.11` reference
implementation (`InMemorySaver`), not from documentation. Two of them are not
guessable and silently corrupt a resume if got wrong — see `aput_writes`.

Deliberate simplification: the full checkpoint is serialised as one blob, including
`channel_values`. `InMemorySaver` splits channel values into separately-versioned
blobs so unchanged values are not rewritten each step. That is a storage optimisation
whose benefit scales with graph size and step count; this deployment has one user and
short workflows, and the split doubles the number of ways a resume can come back
partially wrong.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    RunnableConfig,
    get_checkpoint_id,
    get_checkpoint_metadata,
)

from pca.observability.logging import get_logger
from pca.ports.checkpoints import CheckpointRow, CheckpointStorePort, CheckpointWriteRow

_log = get_logger(__name__)

_SYNC_MESSAGE = (
    "PostgresCheckpointSaver is async-only; use the a-prefixed method. The store "
    "beneath it is asyncpg and has no synchronous path."
)


def _thread(config: RunnableConfig) -> tuple[str, str]:
    configurable = config["configurable"]
    return configurable["thread_id"], configurable.get("checkpoint_ns", "")


class PostgresCheckpointSaver(BaseCheckpointSaver[str]):
    def __init__(
        self, store: CheckpointStorePort, workflow: str | None = None
    ) -> None:
        super().__init__()
        self._store = store
        # Operational label only. Lets an operator see which workflow a stuck thread
        # belongs to without deserialising a blob.
        self._workflow = workflow

    # ------------------------------------------------------------------- reads

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id, checkpoint_ns = _thread(config)
        row = await self._store.get(thread_id, checkpoint_ns, get_checkpoint_id(config))
        if row is None or row.payload is None:
            return None
        return await self._to_tuple(row)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 - name fixed by the base class
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        thread_id: str | None = None
        checkpoint_ns: str | None = None
        if config is not None:
            thread_id, checkpoint_ns = _thread(config)

        rows = await self._store.list(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            before=get_checkpoint_id(before) if before else None,
            limit=limit,
            metadata_filter=filter,
        )
        for row in rows:
            if row.payload is None:
                continue
            yield await self._to_tuple(row)

    # ------------------------------------------------------------------ writes

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id, checkpoint_ns = _thread(config)
        checkpoint_type, payload = self.serde.dumps_typed(checkpoint)

        await self._store.put(
            CheckpointRow(
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                checkpoint_id=checkpoint["id"],
                type=checkpoint_type,
                payload=payload,
                # Stored as JSONB rather than a serialised blob so that `alist`'s
                # filter can be a SQL predicate. Filtering in Python would have to
                # read every checkpoint for the thread before applying `limit`.
                metadata=dict(get_checkpoint_metadata(config, metadata)),
                # The config passed IN carries the parent's id; the checkpoint being
                # written carries its own. Reading the parent from the wrong one
                # produces a self-referential chain and breaks history traversal.
                parent_id=config["configurable"].get("checkpoint_id"),
                workflow=self._workflow,
            )
        )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Persist writes emitted by a task before it was interrupted.

        Two rules here are load-bearing and neither is obvious:

        1. Special channels (`__error__`, `__interrupt__`, `__resume__`,
           `__scheduled__`) take **fixed negative indices** from `WRITES_IDX_MAP`
           rather than their position in the sequence. Using positional indices would
           let an ordinary write collide with an interrupt payload.
        2. Ordinary writes (index >= 0) are **insert-if-absent**; special writes
           overwrite. A resumed task re-emits its writes, and overwriting an ordinary
           write would replace the value recorded at interrupt time with one produced
           by the replay — quietly discarding the state the checkpoint existed to keep.

        Both taken from the `InMemorySaver` reference implementation.
        """
        thread_id, checkpoint_ns = _thread(config)
        checkpoint_id = config["configurable"]["checkpoint_id"]

        rows = [
            CheckpointWriteRow(
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                checkpoint_id=checkpoint_id,
                task_id=task_id,
                idx=WRITES_IDX_MAP.get(channel, index),
                channel=channel,
                type=(serialised := self.serde.dumps_typed(value))[0],
                payload=serialised[1],
                task_path=task_path,
            )
            for index, (channel, value) in enumerate(writes)
        ]
        await self._store.put_writes(rows)

    async def adelete_thread(self, thread_id: str) -> None:
        await self._store.delete_thread(thread_id)

    # --------------------------------------------------------------- internals

    async def _to_tuple(self, row: CheckpointRow) -> CheckpointTuple:
        writes = await self._store.get_writes(
            row.thread_id, row.checkpoint_ns, row.checkpoint_id
        )
        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": row.thread_id,
                    "checkpoint_ns": row.checkpoint_ns,
                    "checkpoint_id": row.checkpoint_id,
                }
            },
            checkpoint=self.serde.loads_typed((row.type or "", row.payload or b"")),
            metadata=row.metadata,  # type: ignore[arg-type]
            parent_config=(
                {
                    "configurable": {
                        "thread_id": row.thread_id,
                        "checkpoint_ns": row.checkpoint_ns,
                        "checkpoint_id": row.parent_id,
                    }
                }
                if row.parent_id
                else None
            ),
            pending_writes=[
                (w.task_id, w.channel, self.serde.loads_typed((w.type or "", w.payload or b"")))
                for w in writes
            ],
        )

    # ------------------------------------------------------------ sync surface
    #
    # Raise rather than inherit the base class's behaviour. Every path in this
    # application is async; a graph compiled somewhere that reached the sync methods
    # would be a wiring mistake, and the useful outcome is a message naming the async
    # equivalent rather than a silent no-op that loses state.

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        raise NotImplementedError(f"{_SYNC_MESSAGE} Use aget_tuple.")

    def list(  # noqa: A003 - name fixed by the base class
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Any:
        raise NotImplementedError(f"{_SYNC_MESSAGE} Use alist.")

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        raise NotImplementedError(f"{_SYNC_MESSAGE} Use aput.")

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        raise NotImplementedError(f"{_SYNC_MESSAGE} Use aput_writes.")

    def delete_thread(self, thread_id: str) -> None:
        raise NotImplementedError(f"{_SYNC_MESSAGE} Use adelete_thread.")
