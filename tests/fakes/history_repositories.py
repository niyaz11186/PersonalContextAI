"""In-memory fakes for the Unit 3 history ports.

`FakeTransactionManager` is the interesting one. A fake that merely handed out a
transaction object would let an "atomicity" test pass without any atomicity: the
repositories are dicts, so a failed commit would leave its partial writes in place
and the test would be asserting nothing.

So it takes snapshots of the fakes it manages and restores them if the body raises.
That makes the rollback real within the fakes, which is what lets a test assert that
a failed commit leaves no trace — the exact property Unit 2 lacked.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Protocol

from pca.domain.enums import BeliefChangeCause, MemoryKind
from pca.domain.history import BeliefTransition, MemoryOperation
from pca.domain.ids import EntityId, MemoryId
from pca.domain.temporal import BeliefWindow


class _Snapshottable(Protocol):
    def snapshot(self) -> Any: ...
    def restore(self, snap: Any) -> None: ...


class _Transaction:
    """Identity marker. Repositories record which transaction each write received."""

    def __init__(self, label: int) -> None:
        self.label = label

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FakeTransaction {self.label}>"


class FakeTransactionManager:
    """Satisfies TransactionManagerPort with genuine rollback across fakes."""

    def __init__(self, *participants: _Snapshottable) -> None:
        self._participants = list(participants)
        self.opened = 0
        self.committed = 0
        self.rolled_back = 0

    def add(self, *participants: _Snapshottable) -> None:
        self._participants.extend(participants)

    @asynccontextmanager
    async def transaction(self):
        self.opened += 1
        tx = _Transaction(self.opened)
        snaps = [(p, p.snapshot()) for p in self._participants]
        try:
            yield tx
        except BaseException:
            for participant, snap in snaps:
                participant.restore(snap)
            self.rolled_back += 1
            raise
        self.committed += 1


class FakeBeliefRepository:
    def __init__(self) -> None:
        self.transitions: list[BeliefTransition] = []
        self.write_transactions: list[Any] = []

    def snapshot(self) -> Any:
        return list(self.transitions)

    def restore(self, snap: Any) -> None:
        self.transitions = list(snap)

    async def record(
        self, transition: BeliefTransition, tx: Any | None = None
    ) -> BeliefTransition:
        self.write_transactions.append(tx)
        self.transitions.append(transition)
        return transition

    async def close_open_transition(
        self,
        memory_id: MemoryId,
        memory_kind: MemoryKind,
        retracted_at: datetime,
        tx: Any | None = None,
    ) -> None:
        self.write_transactions.append(tx)
        for index, existing in enumerate(self.transitions):
            if (
                existing.memory_id == memory_id
                and existing.memory_kind == memory_kind
                and existing.belief.retracted_at is None
            ):
                self.transitions[index] = BeliefTransition(
                    id=existing.id,
                    memory_id=existing.memory_id,
                    memory_kind=existing.memory_kind,
                    cause=existing.cause,
                    belief=BeliefWindow(
                        asserted_at=existing.belief.asserted_at,
                        retracted_at=retracted_at,
                    ),
                    statement=existing.statement,
                    validity=existing.validity,
                    superseded_by=existing.superseded_by,
                    reason=existing.reason,
                    recorded_at=existing.recorded_at,
                )

    async def for_memory(
        self, memory_id: MemoryId, memory_kind: MemoryKind
    ) -> Sequence[BeliefTransition]:
        found = [
            t
            for t in self.transitions
            if t.memory_id == memory_id and t.memory_kind == memory_kind
        ]
        return sorted(found, key=lambda t: (t.belief.asserted_at, t.recorded_at))

    async def believed_at(
        self, when: datetime, limit: int = 200
    ) -> Sequence[BeliefTransition]:
        found = [t for t in self.transitions if t.held_at(when)]
        found.sort(key=lambda t: t.belief.asserted_at, reverse=True)
        return found[:limit]

    async def transitions_between(
        self, start: datetime, end: datetime, causes: Sequence[str] = ()
    ) -> Sequence[BeliefTransition]:
        wanted = set(causes)
        found = [
            t
            for t in self.transitions
            if t.belief.retracted_at is not None
            and start < t.belief.retracted_at <= end
            and (not wanted or t.cause.value in wanted)
        ]
        return sorted(found, key=lambda t: t.belief.retracted_at)  # type: ignore[arg-type,return-value]

    async def count(self) -> int:
        return len(self.transitions)

    # ------------------------------------------------------------ test helpers

    def causes_for(self, memory_id: MemoryId) -> list[BeliefChangeCause]:
        return [t.cause for t in self.transitions if t.memory_id == memory_id]


class FakeOperationLogRepository:
    def __init__(self) -> None:
        self.entries: list[MemoryOperation] = []
        self.write_transactions: list[Any] = []

    def snapshot(self) -> Any:
        return list(self.entries)

    def restore(self, snap: Any) -> None:
        self.entries = list(snap)

    async def append(
        self, operation: MemoryOperation, tx: Any | None = None
    ) -> MemoryOperation:
        self.write_transactions.append(tx)
        self.entries.append(operation)
        return operation

    async def recent(self, limit: int = 50) -> Sequence[MemoryOperation]:
        return list(reversed(self.entries))[:limit]

    async def for_memory(
        self, memory_id: MemoryId, limit: int = 50
    ) -> Sequence[MemoryOperation]:
        found = [e for e in self.entries if e.memory_id == memory_id]
        return list(reversed(found))[:limit]

    async def for_entity(
        self, entity_id: EntityId, limit: int = 50
    ) -> Sequence[MemoryOperation]:
        found = [e for e in self.entries if e.entity_id == entity_id]
        return list(reversed(found))[:limit]

    async def count(self) -> int:
        return len(self.entries)
