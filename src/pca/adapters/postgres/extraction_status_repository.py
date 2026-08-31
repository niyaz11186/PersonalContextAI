"""PostgreSQL implementation of ExtractionStatusRepositoryPort.

Layer L5. SQLAlchemy Core only.

The durable half of the ADR-008 write barrier. `ExtractionCoordinator` keeps an
in-process lock as well, but only as an optimisation: a lock disappears with the
process, and ADR-008 requires that an episode caught mid-extraction by a crash still
be recoverable afterwards.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from pca.adapters.postgres.tables import extraction_status
from pca.domain.enums import ExtractionState
from pca.domain.ids import ConversationId, EpisodeId
from pca.domain.orchestration import ExtractionRecord
from pca.observability.logging import get_logger
from pca.ports.store import RelationalStorePort

_log = get_logger(__name__)

_IN_FLIGHT = (ExtractionState.PENDING.value, ExtractionState.RUNNING.value)

# ABANDONED is included deliberately. It means the barrier stopped waiting, not that
# the work was wrong, so it is still owed a retry.
_RECOVERABLE = (*_IN_FLIGHT, ExtractionState.ABANDONED.value)

_ERROR_LIMIT = 2000


def _to_record(row: Any) -> ExtractionRecord:
    return ExtractionRecord(
        episode_id=EpisodeId(row["episode_id"]),
        conversation_id=(
            ConversationId(row["conversation_id"]) if row["conversation_id"] else None
        ),
        state=ExtractionState(row["state"]),
        attempts=row["attempts"],
        submitted_at=row["submitted_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error=row["error"],
    )


class PostgresExtractionStatusRepository:
    def __init__(self, store: RelationalStorePort) -> None:
        self._store = store

    async def claim(
        self,
        episode_id: EpisodeId,
        conversation_id: ConversationId | None,
        submitted_at: datetime,
    ) -> bool:
        """Take ownership of an episode's extraction. False if already claimed.

        `ON CONFLICT DO NOTHING` on the `episode_id` primary key is the whole
        idempotency mechanism (ADR-008, C-35). Checking for an existing row first and
        then inserting would leave a window between the two in which a concurrent
        submit also finds nothing — and after a crash-recovery restart, concurrent
        submits of the same episode are the expected case rather than a rare race.
        """
        result = await self._store.execute(
            pg_insert(extraction_status)
            .values(
                episode_id=episode_id,
                conversation_id=conversation_id,
                state=ExtractionState.PENDING.value,
                attempts=0,
                submitted_at=submitted_at,
                updated_at=submitted_at,
            )
            .on_conflict_do_nothing(index_elements=["episode_id"])
        )
        claimed = bool(result.rowcount)
        if not claimed:
            _log.info("extraction_already_claimed", episode_id=str(episode_id))
        return claimed

    async def mark_running(self, episode_id: EpisodeId, started_at: datetime) -> None:
        await self._store.execute(
            update(extraction_status)
            .where(extraction_status.c.episode_id == episode_id)
            .values(
                state=ExtractionState.RUNNING.value,
                # Incremented here rather than on claim so the count reflects
                # execution attempts. A row re-queued twice without ever running is
                # not two failures.
                attempts=extraction_status.c.attempts + 1,
                started_at=started_at,
                finished_at=None,
                error=None,
                updated_at=started_at,
            )
        )

    async def mark_finished(
        self,
        episode_id: EpisodeId,
        state: ExtractionState,
        finished_at: datetime,
        error: str | None = None,
    ) -> None:
        await self._store.execute(
            update(extraction_status)
            .where(extraction_status.c.episode_id == episode_id)
            .values(
                state=state.value,
                finished_at=finished_at,
                error=error[:_ERROR_LIMIT] if error else None,
                updated_at=finished_at,
            )
        )

    async def get(self, episode_id: EpisodeId) -> ExtractionRecord | None:
        row = await self._store.fetch_one(
            select(extraction_status).where(
                extraction_status.c.episode_id == episode_id
            )
        )
        return _to_record(row) if row else None

    async def in_flight_for_conversation(
        self, conversation_id: ConversationId
    ) -> Sequence[ExtractionRecord]:
        rows = await self._store.fetch_all(
            select(extraction_status)
            .where(
                extraction_status.c.conversation_id == conversation_id,
                extraction_status.c.state.in_(_IN_FLIGHT),
            )
            .order_by(extraction_status.c.submitted_at)
        )
        return [_to_record(row) for row in rows]

    async def recoverable(self, limit: int = 100) -> Sequence[ExtractionRecord]:
        rows = await self._store.fetch_all(
            select(extraction_status)
            .where(extraction_status.c.state.in_(_RECOVERABLE))
            .order_by(extraction_status.c.submitted_at)
            .limit(limit)
        )
        return [_to_record(row) for row in rows]

    async def count_by_state(self) -> dict[str, int]:
        rows = await self._store.fetch_all(
            select(extraction_status.c.state, func.count().label("n")).group_by(
                extraction_status.c.state
            )
        )
        return {row["state"]: row["n"] for row in rows}
