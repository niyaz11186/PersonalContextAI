"""In-memory ExtractionStatusRepositoryPort for tests.

Models `claim` as insert-if-absent, which is the ADR-008 idempotency guarantee the
real adapter gets from the `episode_id` primary key. A fake that always returned True
would let a duplicate-submit bug pass its own regression test.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pca.domain.enums import ExtractionState
from pca.domain.ids import ConversationId, EpisodeId
from pca.domain.orchestration import ExtractionRecord

_IN_FLIGHT = (ExtractionState.PENDING, ExtractionState.RUNNING)
_RECOVERABLE = (*_IN_FLIGHT, ExtractionState.ABANDONED)


class FakeExtractionStatusRepository:
    def __init__(self) -> None:
        self.records: dict[EpisodeId, ExtractionRecord] = {}

    async def claim(
        self,
        episode_id: EpisodeId,
        conversation_id: ConversationId | None,
        submitted_at: datetime,
    ) -> bool:
        if episode_id in self.records:
            return False
        self.records[episode_id] = ExtractionRecord(
            episode_id=episode_id,
            conversation_id=conversation_id,
            state=ExtractionState.PENDING,
            attempts=0,
            submitted_at=submitted_at,
            updated_at=submitted_at,
        )
        return True

    async def mark_running(self, episode_id: EpisodeId, started_at: datetime) -> None:
        existing = self.records[episode_id]
        self.records[episode_id] = ExtractionRecord(
            episode_id=existing.episode_id,
            conversation_id=existing.conversation_id,
            state=ExtractionState.RUNNING,
            attempts=existing.attempts + 1,
            submitted_at=existing.submitted_at,
            updated_at=started_at,
            started_at=started_at,
        )

    async def mark_finished(
        self,
        episode_id: EpisodeId,
        state: ExtractionState,
        finished_at: datetime,
        error: str | None = None,
    ) -> None:
        existing = self.records[episode_id]
        self.records[episode_id] = ExtractionRecord(
            episode_id=existing.episode_id,
            conversation_id=existing.conversation_id,
            state=state,
            attempts=existing.attempts,
            submitted_at=existing.submitted_at,
            updated_at=finished_at,
            started_at=existing.started_at,
            finished_at=finished_at,
            error=error,
        )

    async def get(self, episode_id: EpisodeId) -> ExtractionRecord | None:
        return self.records.get(episode_id)

    async def in_flight_for_conversation(
        self, conversation_id: ConversationId
    ) -> Sequence[ExtractionRecord]:
        return [
            r
            for r in self.records.values()
            if r.conversation_id == conversation_id and r.state in _IN_FLIGHT
        ]

    async def recoverable(self, limit: int = 100) -> Sequence[ExtractionRecord]:
        matching = [r for r in self.records.values() if r.state in _RECOVERABLE]
        matching.sort(key=lambda r: r.submitted_at)
        return matching[:limit]

    async def count_by_state(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records.values():
            counts[record.state.value] = counts.get(record.state.value, 0) + 1
        return counts
