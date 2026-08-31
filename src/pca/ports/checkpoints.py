"""CheckpointStorePort — durable storage for interrupted workflow state.

Layer L4.

This port exists to keep two boundary rules from colliding. Rule 2 says only
`pca.orchestration` may import `langgraph`; rule 3 says storage libraries stay out of
the domain. A checkpointer needs both LangGraph's types and SQL, so it is split:

    orchestration/checkpointer.py    LangGraph types, no sqlalchemy
    ports/checkpoints.py             this file — neither
    adapters/postgres/…              sqlalchemy, no langgraph

Everything crossing this seam is `str`, `bytes`, `int` or a plain dataclass. Nothing
here knows what a `Checkpoint` is, which is also what makes the store testable
without constructing LangGraph objects.

`langgraph-checkpoint-postgres` was rejected for the same reason the seam exists: it
requires `psycopg`, which would add a second PostgreSQL driver and connection pool
beside the existing asyncpg one.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class CheckpointRow:
    """One serialised checkpoint.

    `payload` is bytes rather than a mapping because the LangGraph serialiser emits
    `(type, bytes)` and re-encoding that as JSON would be a lossy round trip through
    a representation it never asked for.
    """

    thread_id: str
    checkpoint_ns: str
    checkpoint_id: str
    type: str | None
    payload: bytes | None
    metadata: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None
    workflow: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CheckpointWriteRow:
    """A write emitted by a task before it was interrupted.

    Without these a resume restarts the interrupted task from scratch rather than
    continuing it, silently discarding whatever it had already produced.
    """

    thread_id: str
    checkpoint_ns: str
    checkpoint_id: str
    task_id: str
    idx: int
    channel: str
    type: str | None
    payload: bytes | None
    task_path: str = ""


class CheckpointStorePort(Protocol):
    async def put(self, row: CheckpointRow) -> None:
        """Insert or replace a checkpoint.

        Upsert rather than insert: LangGraph may write the same checkpoint id twice
        within a step, and a duplicate-key failure there would abort a graph run over
        a condition the framework treats as ordinary.
        """
        ...

    async def get(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str | None
    ) -> CheckpointRow | None:
        """Fetch one checkpoint, or the most recent when `checkpoint_id` is None."""
        ...

    async def list(
        self,
        thread_id: str | None,
        checkpoint_ns: str | None = None,
        before: str | None = None,
        limit: int | None = None,
        metadata_filter: dict[str, Any] | None = None,
    ) -> Sequence[CheckpointRow]:
        """Checkpoints newest first, for history and time travel.

        `metadata_filter` is applied in the store rather than by the caller because it
        must be combined with `limit`. Filtering after the fact would have to read
        every checkpoint on the thread to return the last few that match.
        """
        ...

    async def put_writes(self, writes: Sequence[CheckpointWriteRow]) -> None:
        """Persist pending writes.

        Rows with a negative `idx` are special channels (interrupt, resume, error) and
        must overwrite. Rows with `idx >= 0` are ordinary writes and must NOT: a
        resumed task re-emits them, and overwriting would replace the value captured
        at interrupt time with one produced by the replay.
        """
        ...

    async def get_writes(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> Sequence[CheckpointWriteRow]: ...

    async def delete_thread(self, thread_id: str) -> None:
        """Remove a thread's checkpoints and its pending writes.

        Both, in one call. Deleting checkpoints while leaving writes behind would
        leave orphans that a later thread reusing the id would silently inherit.
        """
        ...
