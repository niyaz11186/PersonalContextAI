"""ProvenanceService — the chain from derived memory back to source (FR-02.5).

Layer L3.

Provenance is stored many-to-many, which is not incidental. ADR-012's corroboration
rule depends on being able to ask "how many sources still support this fact?" before
deciding whether deleting one should retract it. A single source column would force
a choice between orphaning facts and destroying knowledge that still has evidence.
"""

from __future__ import annotations

from collections.abc import Sequence

from pca.domain.conversation import Message, SourceExcerpt
from pca.domain.enums import MemoryKind
from pca.domain.ids import EpisodeId, MemoryId
from pca.domain.memory import ProvenanceRef
from pca.observability.logging import get_logger
from pca.ports.clock import ClockPort
from pca.ports.repositories import ConversationRepositoryPort, ProvenanceRepositoryPort

_log = get_logger(__name__)


class ProvenanceService:
    def __init__(
        self,
        repository: ProvenanceRepositoryPort,
        conversations: ConversationRepositoryPort,
        clock: ClockPort,
    ) -> None:
        self._repository = repository
        self._conversations = conversations
        self._clock = clock

    async def record(
        self, memory_id: MemoryId, kind: MemoryKind, ref: ProvenanceRef
    ) -> None:
        await self._repository.record(
            memory_id=memory_id,
            memory_kind=kind,
            ref=ref,
            recorded_at=self._clock.now(),
        )

    async def chain(self, memory_id: MemoryId, kind: MemoryKind) -> Sequence[ProvenanceRef]:
        """Every source that supports this memory."""
        return await self._repository.for_memory(memory_id, kind)

    async def supporting_source_count(self, memory_id: MemoryId, kind: MemoryKind) -> int:
        """How many sources remain. The corroboration rule reads this (ADR-012)."""
        return await self._repository.count_for_memory(memory_id, kind)

    async def source_excerpt(self, ref: ProvenanceRef, window: int = 2) -> SourceExcerpt:
        """Surrounding messages for a provenance reference.

        Returns context rather than an isolated sentence. A fact shown without its
        surroundings is hard to judge — "she was furious" reads very differently
        depending on what came immediately before it.
        """
        if ref.message_id is None:
            return SourceExcerpt(messages=[])

        messages: Sequence[Message] = await self._conversations.get_surrounding(
            ref.message_id, window
        )
        return SourceExcerpt(
            messages=list(messages), highlight_message_id=ref.message_id
        )

    async def memories_from_episode(
        self, episode_id: EpisodeId
    ) -> Sequence[tuple[MemoryId, MemoryKind]]:
        """Everything derived from one episode.

        Used by source deletion to find what a removal would affect (ADR-012).
        """
        return await self._repository.memories_from_episode(episode_id)
