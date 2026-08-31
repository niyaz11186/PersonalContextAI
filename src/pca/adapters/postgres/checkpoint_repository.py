"""PostgreSQL implementation of CheckpointStorePort.

Layer L5. SQLAlchemy Core only — **no langgraph import in this file**. The LangGraph
side of the checkpointer lives in `orchestration/checkpointer.py`; this half only
moves bytes.

Ordering note: checkpoint ids are monotonically increasing strings (LangGraph uses
UUIDv6-style ids), so lexical ordering is chronological. That is what makes "the
latest checkpoint for this thread" a plain `ORDER BY checkpoint_id DESC` rather than
a timestamp comparison, which would be ambiguous for two checkpoints written in the
same instant during one graph step.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from pca.adapters.postgres.tables import (
    workflow_checkpoint_writes,
    workflow_checkpoints,
)
from pca.observability.logging import get_logger
from pca.ports.checkpoints import CheckpointRow, CheckpointWriteRow
from pca.ports.store import RelationalStorePort

_log = get_logger(__name__)

_WRITE_KEY = ["thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"]


def _write_values(w: CheckpointWriteRow) -> dict[str, Any]:
    return {
        "thread_id": w.thread_id,
        "checkpoint_ns": w.checkpoint_ns,
        "checkpoint_id": w.checkpoint_id,
        "task_id": w.task_id,
        "idx": w.idx,
        "channel": w.channel,
        "type": w.type,
        "payload": w.payload,
        "task_path": w.task_path,
    }


def _to_row(row: Any) -> CheckpointRow:
    return CheckpointRow(
        thread_id=row["thread_id"],
        checkpoint_ns=row["checkpoint_ns"],
        checkpoint_id=row["checkpoint_id"],
        type=row["type"],
        payload=bytes(row["payload"]) if row["payload"] is not None else None,
        metadata=row["metadata"] or {},
        parent_id=row["parent_id"],
        workflow=row["workflow"],
        created_at=row["created_at"],
    )


def _to_write(row: Any) -> CheckpointWriteRow:
    return CheckpointWriteRow(
        thread_id=row["thread_id"],
        checkpoint_ns=row["checkpoint_ns"],
        checkpoint_id=row["checkpoint_id"],
        task_id=row["task_id"],
        idx=row["idx"],
        channel=row["channel"],
        type=row["type"],
        payload=bytes(row["payload"]) if row["payload"] is not None else None,
        task_path=row["task_path"],
    )


class PostgresCheckpointStore:
    def __init__(self, store: RelationalStorePort) -> None:
        self._store = store

    async def put(self, row: CheckpointRow) -> None:
        values: dict[str, Any] = {
            "thread_id": row.thread_id,
            "checkpoint_ns": row.checkpoint_ns,
            "checkpoint_id": row.checkpoint_id,
            "parent_id": row.parent_id,
            "workflow": row.workflow,
            "metadata": row.metadata,
            "type": row.type,
            "payload": row.payload,
        }
        # Omitted rather than passed as None when absent: the column is NOT NULL with
        # a now() default, so an explicit NULL would fail the insert.
        if row.created_at is not None:
            values["created_at"] = row.created_at

        statement = pg_insert(workflow_checkpoints).values(**values)
        await self._store.execute(
            # Upsert: LangGraph can write the same checkpoint id more than once
            # within a step, and a duplicate-key error there would abort a graph run
            # over something the framework treats as ordinary. created_at is excluded
            # from the update so it keeps recording first write, not last.
            statement.on_conflict_do_update(
                index_elements=["thread_id", "checkpoint_ns", "checkpoint_id"],
                set_={
                    "parent_id": statement.excluded.parent_id,
                    "workflow": statement.excluded.workflow,
                    # Subscripted because `metadata` is a reserved attribute name on
                    # SQLAlchemy collections and attribute access would resolve to
                    # the MetaData object rather than the column.
                    "metadata": statement.excluded["metadata"],
                    "type": statement.excluded.type,
                    "payload": statement.excluded.payload,
                },
            )
        )

    async def get(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str | None
    ) -> CheckpointRow | None:
        query = select(workflow_checkpoints).where(
            workflow_checkpoints.c.thread_id == thread_id,
            workflow_checkpoints.c.checkpoint_ns == checkpoint_ns,
        )
        if checkpoint_id is not None:
            query = query.where(workflow_checkpoints.c.checkpoint_id == checkpoint_id)
        else:
            query = query.order_by(workflow_checkpoints.c.checkpoint_id.desc()).limit(1)

        row = await self._store.fetch_one(query)
        return _to_row(row) if row else None

    async def list(
        self,
        thread_id: str | None,
        checkpoint_ns: str | None = None,
        before: str | None = None,
        limit: int | None = None,
        metadata_filter: dict[str, Any] | None = None,
    ) -> Sequence[CheckpointRow]:
        query = select(workflow_checkpoints)
        if thread_id is not None:
            query = query.where(workflow_checkpoints.c.thread_id == thread_id)
        if checkpoint_ns is not None:
            query = query.where(workflow_checkpoints.c.checkpoint_ns == checkpoint_ns)
        if before is not None:
            query = query.where(workflow_checkpoints.c.checkpoint_id < before)
        if metadata_filter:
            # JSONB containment, so the filter combines with LIMIT in one query.
            query = query.where(
                workflow_checkpoints.c["metadata"].contains(metadata_filter)
            )

        query = query.order_by(workflow_checkpoints.c.checkpoint_id.desc())
        if limit is not None:
            query = query.limit(limit)

        rows = await self._store.fetch_all(query)
        return [_to_row(row) for row in rows]

    async def put_writes(self, writes: Sequence[CheckpointWriteRow]) -> None:
        """Insert ordinary writes, upsert special ones.

        The split is not a micro-optimisation. A resumed task re-emits its writes; if
        ordinary writes overwrote, the value captured at interrupt time would be
        replaced by one produced during replay, discarding exactly the state the
        checkpoint was taken to preserve. Special channels (negative idx, from
        `WRITES_IDX_MAP`) are the opposite case — the latest interrupt or resume
        payload is the correct one.
        """
        if not writes:
            return

        ordinary = [w for w in writes if w.idx >= 0]
        special = [w for w in writes if w.idx < 0]

        if ordinary:
            await self._store.execute(
                pg_insert(workflow_checkpoint_writes)
                .values([_write_values(w) for w in ordinary])
                .on_conflict_do_nothing(index_elements=_WRITE_KEY)
            )

        if special:
            statement = pg_insert(workflow_checkpoint_writes).values(
                [_write_values(w) for w in special]
            )
            await self._store.execute(
                statement.on_conflict_do_update(
                    index_elements=_WRITE_KEY,
                    set_={
                        "channel": statement.excluded.channel,
                        "type": statement.excluded.type,
                        "payload": statement.excluded.payload,
                    },
                )
            )

    async def get_writes(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> Sequence[CheckpointWriteRow]:
        rows = await self._store.fetch_all(
            select(workflow_checkpoint_writes)
            .where(
                workflow_checkpoint_writes.c.thread_id == thread_id,
                workflow_checkpoint_writes.c.checkpoint_ns == checkpoint_ns,
                workflow_checkpoint_writes.c.checkpoint_id == checkpoint_id,
            )
            # idx is emission order within a task; replaying out of order would apply
            # a later write before the one it was meant to follow.
            .order_by(
                workflow_checkpoint_writes.c.task_id,
                workflow_checkpoint_writes.c.idx,
            )
        )
        return [_to_write(row) for row in rows]

    async def delete_thread(self, thread_id: str) -> None:
        async with self._store.transaction() as tx:
            await tx.execute(
                delete(workflow_checkpoint_writes).where(
                    workflow_checkpoint_writes.c.thread_id == thread_id
                )
            )
            await tx.execute(
                delete(workflow_checkpoints).where(
                    workflow_checkpoints.c.thread_id == thread_id
                )
            )
        _log.info("checkpoint_thread_deleted", thread_id=thread_id)
