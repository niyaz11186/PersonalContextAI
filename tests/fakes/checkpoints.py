"""In-memory CheckpointStorePort for tests.

Models the two behaviours that are easy to get wrong in the real adapter, so that a
test asserting them is meaningful rather than tautological:

  * ordinary writes (idx >= 0) are insert-if-absent
  * special writes (idx < 0, from WRITES_IDX_MAP) overwrite

A fake that upserted everything would let the PostgreSQL adapter regress to the same
bug and still pass.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pca.ports.checkpoints import CheckpointRow, CheckpointWriteRow


class FakeCheckpointStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], CheckpointRow] = {}
        self.writes: dict[tuple[str, str, str, str, int], CheckpointWriteRow] = {}

    async def put(self, row: CheckpointRow) -> None:
        self.rows[(row.thread_id, row.checkpoint_ns, row.checkpoint_id)] = row

    async def get(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str | None
    ) -> CheckpointRow | None:
        if checkpoint_id is not None:
            return self.rows.get((thread_id, checkpoint_ns, checkpoint_id))

        candidates = [
            row
            for row in self.rows.values()
            if row.thread_id == thread_id and row.checkpoint_ns == checkpoint_ns
        ]
        if not candidates:
            return None
        # Checkpoint ids are monotonic, so lexical max is the most recent.
        return max(candidates, key=lambda r: r.checkpoint_id)

    async def list(
        self,
        thread_id: str | None,
        checkpoint_ns: str | None = None,
        before: str | None = None,
        limit: int | None = None,
        metadata_filter: dict[str, Any] | None = None,
    ) -> Sequence[CheckpointRow]:
        rows = list(self.rows.values())
        if thread_id is not None:
            rows = [r for r in rows if r.thread_id == thread_id]
        if checkpoint_ns is not None:
            rows = [r for r in rows if r.checkpoint_ns == checkpoint_ns]
        if before is not None:
            rows = [r for r in rows if r.checkpoint_id < before]
        if metadata_filter:
            rows = [
                r
                for r in rows
                if all(r.metadata.get(k) == v for k, v in metadata_filter.items())
            ]

        rows.sort(key=lambda r: r.checkpoint_id, reverse=True)
        return rows[:limit] if limit is not None else rows

    async def put_writes(self, writes: Sequence[CheckpointWriteRow]) -> None:
        for write in writes:
            key = (
                write.thread_id,
                write.checkpoint_ns,
                write.checkpoint_id,
                write.task_id,
                write.idx,
            )
            if write.idx >= 0 and key in self.writes:
                continue
            self.writes[key] = write

    async def get_writes(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> Sequence[CheckpointWriteRow]:
        matching = [
            w
            for (t, ns, cid, _task, _idx), w in self.writes.items()
            if t == thread_id and ns == checkpoint_ns and cid == checkpoint_id
        ]
        matching.sort(key=lambda w: (w.task_id, w.idx))
        return matching

    async def delete_thread(self, thread_id: str) -> None:
        self.rows = {k: v for k, v in self.rows.items() if k[0] != thread_id}
        self.writes = {k: v for k, v in self.writes.items() if k[0] != thread_id}
