"""PostgreSQL implementation of EpisodeRepositoryPort.

Layer L5.

Episodes are the replay source that makes ADR-005 real. The graph is disposable
only because the exact payload sent to it is durable here.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, insert, select, update

from pca.adapters.postgres.tables import episodes
from pca.domain.conversation import Episode
from pca.domain.ids import ConversationId, EpisodeId, MessageId
from pca.ports.clock import ClockPort
from pca.ports.store import RelationalStorePort


def _to_episode(row) -> Episode:  # type: ignore[no-untyped-def]
    return Episode(
        id=EpisodeId(row["id"]),
        content=row["content"],
        occurred_at=row["occurred_at"],
        zone=row["zone"],
        conversation_id=(
            ConversationId(row["conversation_id"]) if row["conversation_id"] else None
        ),
        message_id=MessageId(row["message_id"]) if row["message_id"] else None,
        document_id=row["document_id"],
        ingested_at=row["ingested_at"],
    )


class PostgresEpisodeRepository:
    def __init__(self, store: RelationalStorePort, clock: ClockPort) -> None:
        self._store = store
        self._clock = clock

    async def save(self, episode: Episode, llm_model: str, embedding_model: str) -> Episode:
        """Persist before graph ingestion.

        The model identifiers are recorded per episode because embeddings from
        different models are not comparable. Without them a model change produces
        silently degraded retrieval with no way to detect the mismatch (ADR-013).
        """
        await self._store.execute(
            insert(episodes).values(
                id=episode.id,
                content=episode.content,
                occurred_at=episode.occurred_at,
                zone=episode.zone,
                conversation_id=episode.conversation_id,
                message_id=episode.message_id,
                document_id=episode.document_id,
                created_at=self._clock.now(),
                ingested_at=None,
                llm_model=llm_model,
                embedding_model=embedding_model,
            )
        )
        return episode

    async def get(self, episode_id: EpisodeId) -> Episode | None:
        row = await self._store.fetch_one(select(episodes).where(episodes.c.id == episode_id))
        return _to_episode(row) if row else None

    async def mark_ingested(self, episode_id: EpisodeId, ingested_at: datetime) -> None:
        """Advance the replay watermark.

        Idempotent by only setting a NULL watermark, so a retried ingestion does
        not overwrite the original timestamp.
        """
        await self._store.execute(
            update(episodes)
            .where(episodes.c.id == episode_id, episodes.c.ingested_at.is_(None))
            .values(ingested_at=ingested_at)
        )

    async def pending(self, limit: int) -> Sequence[Episode]:
        """Episodes the graph has not accepted.

        Read at startup to recover work lost to a crash (ADR-008) and by reindex
        to resume.
        """
        rows = await self._store.fetch_all(
            select(episodes)
            .where(episodes.c.ingested_at.is_(None))
            .order_by(episodes.c.created_at)
            .limit(limit)
        )
        return [_to_episode(row) for row in rows]

    async def replay_batch(self, after: datetime | None, limit: int) -> Sequence[Episode]:
        """Ordered episodes for rebuilding the graph from source.

        Ordered by occurred_at, not created_at: replay must reconstruct history in
        the order events happened, otherwise temporal invalidation is applied in
        the wrong sequence and supersession resolves incorrectly.
        """
        statement = select(episodes).order_by(episodes.c.occurred_at).limit(limit)
        if after is not None:
            statement = (
                select(episodes)
                .where(episodes.c.occurred_at > after)
                .order_by(episodes.c.occurred_at)
                .limit(limit)
            )
        rows = await self._store.fetch_all(statement)
        return [_to_episode(row) for row in rows]

    async def count(self) -> int:
        row = await self._store.fetch_one(select(func.count()).select_from(episodes))
        if row is None:
            return 0
        return int(next(iter(row.values())))
