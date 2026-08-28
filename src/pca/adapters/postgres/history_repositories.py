"""PostgreSQL implementations of the Unit 3 history ports.

Layer L5. SQLAlchemy Core only.

Both repositories here are append-only by construction: neither exposes a delete, and
`BeliefRepository` exposes exactly one update — closing an open belief window, which
is how a window *ends* rather than how history is rewritten.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, insert, or_, select, update

from pca.adapters.postgres.scope import scope
from pca.adapters.postgres.tables import belief_history, memory_operations
from pca.domain.enums import BeliefChangeCause, MemoryKind, OperationKind
from pca.domain.history import BeliefTransition, MemoryOperation
from pca.domain.ids import (
    EntityId,
    EpisodeId,
    MemoryId,
    OperationId,
)
from pca.domain.temporal import BeliefWindow, TemporalValidity
from pca.observability.logging import get_logger
from pca.ports.store import RelationalStorePort, Transaction

_log = get_logger(__name__)


def _to_transition(row: Any) -> BeliefTransition:
    return BeliefTransition(
        id=MemoryId(row["id"]),
        memory_id=MemoryId(row["memory_id"]),
        memory_kind=MemoryKind(row["memory_kind"]),
        cause=BeliefChangeCause(row["cause"]),
        belief=BeliefWindow(
            asserted_at=row["asserted_at"], retracted_at=row["retracted_at"]
        ),
        statement=row["statement"],
        validity=TemporalValidity(
            valid_from=row["valid_from"], valid_to=row["valid_to"]
        ),
        superseded_by=(
            MemoryId(row["superseded_by"]) if row["superseded_by"] else None
        ),
        reason=row["reason"],
        recorded_at=row["recorded_at"],
    )


class PostgresBeliefRepository:
    """Belief history. Append-only.

    The one mutation permitted is closing an open window, because a belief window is
    open-ended until something ends it. Everything else is an insert.
    """

    def __init__(self, store: RelationalStorePort) -> None:
        self._store = store

    async def record(
        self, transition: BeliefTransition, tx: Transaction | None = None
    ) -> BeliefTransition:
        async with scope(self._store, tx) as t:
            await t.execute(
                insert(belief_history).values(
                    id=transition.id,
                    memory_id=transition.memory_id,
                    memory_kind=transition.memory_kind.value,
                    cause=transition.cause.value,
                    asserted_at=transition.belief.asserted_at,
                    retracted_at=transition.belief.retracted_at,
                    statement=transition.statement,
                    valid_from=transition.validity.valid_from,
                    valid_to=transition.validity.valid_to,
                    superseded_by=transition.superseded_by,
                    reason=transition.reason,
                    recorded_at=transition.recorded_at,
                )
            )
        return transition

    async def close_open_transition(
        self,
        memory_id: MemoryId,
        memory_kind: MemoryKind,
        retracted_at: datetime,
        tx: Transaction | None = None,
    ) -> None:
        """Close whichever belief window for this memory is still open.

        Guarded by `retracted_at IS NULL` so it is idempotent and cannot retroactively
        move an already-closed window. Without this guard, a repeated correction would
        rewrite the end of an earlier belief and the trail would stop reflecting what
        actually happened.
        """
        async with scope(self._store, tx) as t:
            await t.execute(
                update(belief_history)
                .where(
                    and_(
                        belief_history.c.memory_id == memory_id,
                        belief_history.c.memory_kind == memory_kind.value,
                        belief_history.c.retracted_at.is_(None),
                    )
                )
                .values(retracted_at=retracted_at)
            )

    async def for_memory(
        self, memory_id: MemoryId, memory_kind: MemoryKind
    ) -> Sequence[BeliefTransition]:
        rows = await self._store.fetch_all(
            select(belief_history)
            .where(
                and_(
                    belief_history.c.memory_id == memory_id,
                    belief_history.c.memory_kind == memory_kind.value,
                )
            )
            .order_by(belief_history.c.asserted_at, belief_history.c.recorded_at)
        )
        return [_to_transition(row) for row in rows]

    async def believed_at(
        self, when: datetime, limit: int = 200
    ) -> Sequence[BeliefTransition]:
        """Beliefs held at `when`: window started at or before, had not yet ended.

        This deliberately ignores world-time validity. The question is what the system
        *thought*, not what was true — a fact believed in March and corrected in April
        still appears here for a March timestamp, which is the whole point.
        """
        rows = await self._store.fetch_all(
            select(belief_history)
            .where(
                and_(
                    belief_history.c.asserted_at <= when,
                    or_(
                        belief_history.c.retracted_at.is_(None),
                        belief_history.c.retracted_at > when,
                    ),
                )
            )
            .order_by(belief_history.c.asserted_at.desc())
            .limit(limit)
        )
        return [_to_transition(row) for row in rows]

    async def transitions_between(
        self, start: datetime, end: datetime, causes: Sequence[str] = ()
    ) -> Sequence[BeliefTransition]:
        predicates = [
            belief_history.c.retracted_at.is_not(None),
            belief_history.c.retracted_at > start,
            belief_history.c.retracted_at <= end,
        ]
        if causes:
            predicates.append(belief_history.c.cause.in_(list(causes)))

        rows = await self._store.fetch_all(
            select(belief_history)
            .where(and_(*predicates))
            .order_by(belief_history.c.retracted_at)
        )
        return [_to_transition(row) for row in rows]

    async def count(self) -> int:
        row = await self._store.fetch_one(
            select(func.count()).select_from(belief_history)
        )
        return int(next(iter(row.values()))) if row else 0


class PostgresOperationLogRepository:
    """The audit log. Append and read only — no update, no delete."""

    def __init__(self, store: RelationalStorePort) -> None:
        self._store = store

    async def append(
        self, operation: MemoryOperation, tx: Transaction | None = None
    ) -> MemoryOperation:
        async with scope(self._store, tx) as t:
            await t.execute(
                insert(memory_operations).values(
                    id=operation.id,
                    operation=operation.operation.value,
                    memory_id=operation.memory_id,
                    memory_kind=(
                        operation.memory_kind.value if operation.memory_kind else None
                    ),
                    entity_id=operation.entity_id,
                    episode_id=operation.episode_id,
                    reason=operation.reason,
                    detail=operation.detail or None,
                    performed_at=operation.performed_at,
                )
            )
        return operation

    async def recent(self, limit: int = 50) -> Sequence[MemoryOperation]:
        rows = await self._store.fetch_all(
            select(memory_operations)
            .order_by(memory_operations.c.performed_at.desc())
            .limit(limit)
        )
        return [self._to_operation(row) for row in rows]

    async def for_memory(
        self, memory_id: MemoryId, limit: int = 50
    ) -> Sequence[MemoryOperation]:
        rows = await self._store.fetch_all(
            select(memory_operations)
            .where(memory_operations.c.memory_id == memory_id)
            .order_by(memory_operations.c.performed_at.desc())
            .limit(limit)
        )
        return [self._to_operation(row) for row in rows]

    async def for_entity(
        self, entity_id: EntityId, limit: int = 50
    ) -> Sequence[MemoryOperation]:
        rows = await self._store.fetch_all(
            select(memory_operations)
            .where(memory_operations.c.entity_id == entity_id)
            .order_by(memory_operations.c.performed_at.desc())
            .limit(limit)
        )
        return [self._to_operation(row) for row in rows]

    async def count(self) -> int:
        row = await self._store.fetch_one(
            select(func.count()).select_from(memory_operations)
        )
        return int(next(iter(row.values()))) if row else 0

    def _to_operation(self, row: Any) -> MemoryOperation:
        return MemoryOperation(
            id=OperationId(row["id"]),
            operation=OperationKind(row["operation"]),
            performed_at=row["performed_at"],
            memory_id=MemoryId(row["memory_id"]) if row["memory_id"] else None,
            memory_kind=(
                MemoryKind(row["memory_kind"]) if row["memory_kind"] else None
            ),
            entity_id=EntityId(row["entity_id"]) if row["entity_id"] else None,
            episode_id=EpisodeId(row["episode_id"]) if row["episode_id"] else None,
            reason=row["reason"],
            detail=dict(row["detail"]) if row["detail"] else {},
        )
