"""MemoryOperationLog — the audit trail for memory mutations.

Layer L3.

Specification §12 requires memory changes to be auditable, and ADR-014 requires entity
merges to be reversible. Both reduce to the same requirement: the mutation must have
been recorded at the time it happened, in the same transaction that made it.

Every method takes `tx` and none of them opens its own transaction. That is
deliberate. An audit entry written in a separate transaction from the change it
describes can succeed when the change fails, or fail when the change succeeds, and
both outcomes produce a log that lies.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from pca.domain.enums import MemoryKind, OperationKind
from pca.domain.history import MemoryOperation
from pca.domain.ids import EntityId, EpisodeId, MemoryId, OperationId
from pca.observability.logging import get_logger
from pca.ports.clock import ClockPort
from pca.ports.repositories import OperationLogRepositoryPort
from pca.ports.store import Transaction

_log = get_logger(__name__)


class MemoryOperationLog:
    def __init__(
        self, repository: OperationLogRepositoryPort, clock: ClockPort
    ) -> None:
        self._repository = repository
        self._clock = clock

    async def record(
        self,
        operation: OperationKind,
        *,
        memory_id: MemoryId | None = None,
        memory_kind: MemoryKind | None = None,
        entity_id: EntityId | None = None,
        episode_id: EpisodeId | None = None,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
        tx: Transaction | None = None,
    ) -> MemoryOperation:
        entry = MemoryOperation(
            id=OperationId(uuid4()),
            operation=operation,
            performed_at=self._clock.now(),
            memory_id=memory_id,
            memory_kind=memory_kind,
            entity_id=entity_id,
            episode_id=episode_id,
            reason=reason,
            detail=detail or {},
        )
        await self._repository.append(entry, tx=tx)
        _log.info(
            "memory_operation_recorded",
            operation=operation.value,
            memory_id=str(memory_id) if memory_id else None,
            entity_id=str(entity_id) if entity_id else None,
            reason=reason,
        )
        return entry

    # ------------------------------------------------------------------- reads

    async def recent(self, limit: int = 50) -> Sequence[MemoryOperation]:
        return await self._repository.recent(limit)

    async def history_for(
        self, memory_id: MemoryId, limit: int = 50
    ) -> Sequence[MemoryOperation]:
        """Everything that has happened to one memory."""
        return await self._repository.for_memory(memory_id, limit)

    async def history_for_entity(
        self, entity_id: EntityId, limit: int = 50
    ) -> Sequence[MemoryOperation]:
        """Everything that has happened to one entity, including merges.

        This is what makes an accidental merge recoverable: the absorbed id and the
        reason are both in the log, so the operation can be described and undone.
        """
        return await self._repository.for_entity(entity_id, limit)

    async def count(self) -> int:
        return await self._repository.count()
