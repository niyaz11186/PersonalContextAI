"""EpisodeService — the ADR-005 write path.

Layer L3.

This is the service that makes "Neo4j is disposable" true rather than aspirational.
The ordering is the whole point:

    1. Persist the episode to PostgreSQL          <- durability point
    2. Ingest it into the graph
    3. Mark the replay watermark

If step 2 or 3 fails, the episode is still durable and will be retried or replayed.
If the order were reversed, a graph write followed by a PostgreSQL failure would
leave memory that cannot be rebuilt — and the architecture's central safety
property would be gone.
"""

from __future__ import annotations

from uuid import uuid4

from pca.domain.conversation import Episode, Message
from pca.domain.errors import MemoryGraphUnavailable
from pca.domain.ids import EpisodeId
from pca.observability.logging import get_logger
from pca.ports.clock import ClockPort
from pca.ports.graph import MemoryGraphPort
from pca.ports.repositories import EpisodeRepositoryPort

_log = get_logger(__name__)


class EpisodeService:
    def __init__(
        self,
        repository: EpisodeRepositoryPort,
        graph: MemoryGraphPort,
        clock: ClockPort,
        llm_model: str,
        embedding_model: str,
    ) -> None:
        self._repository = repository
        self._graph = graph
        self._clock = clock
        self._llm_model = llm_model
        self._embedding_model = embedding_model

    async def record_message(self, message: Message) -> Episode:
        """Turn a message into a durable episode.

        `occurred_at` and `zone` are copied from the message rather than read from
        the clock, because this may run in the background minutes after the message
        was sent (ADR-008). Using clock time here would shift the anchor that every
        relative time reference in the text resolves against.
        """
        episode = Episode(
            id=EpisodeId(uuid4()),
            content=message.content,
            occurred_at=message.captured_at,
            zone=message.zone,
            conversation_id=message.conversation_id,
            message_id=message.id,
        )
        await self._repository.save(
            episode, llm_model=self._llm_model, embedding_model=self._embedding_model
        )
        _log.info(
            "episode_persisted",
            episode_id=str(episode.id),
            message_id=str(message.id),
        )
        return episode

    async def ingest(self, episode: Episode) -> bool:
        """Push a persisted episode into the graph and advance the watermark.

        Returns False rather than raising when the graph is unavailable: the
        episode is already durable, so this is a retryable condition and not a
        request failure. Contrast with PostgreSQL, which has no degradation path
        (constraint C-22).
        """
        try:
            result = await self._graph.add_episode(episode)
        except Exception as exc:  # noqa: BLE001 - translated to a domain condition
            _log.warning(
                "episode_ingest_failed",
                episode_id=str(episode.id),
                error=str(exc)[:200],
            )
            return False

        await self._repository.mark_ingested(episode.id, self._clock.now())
        _log.info(
            "episode_ingested",
            episode_id=str(episode.id),
            graph_ref=result.episode_ref,
        )
        return True

    async def record_and_ingest(self, message: Message) -> Episode:
        """Convenience for the common path. Persist, then attempt ingestion."""
        episode = await self.record_message(message)
        await self.ingest(episode)
        return episode

    async def recover_pending(self, limit: int = 100) -> list[EpisodeId]:
        """Re-ingest episodes left unmarked by a crash.

        Called at startup. Without this, an episode persisted but never ingested
        would be invisible to retrieval until a full reindex — durable but unused.
        """
        pending = await self._repository.pending(limit)
        if not pending:
            return []

        _log.info("recovering_pending_episodes", count=len(pending))
        recovered: list[EpisodeId] = []
        for episode in pending:
            if await self.ingest(episode):
                recovered.append(episode.id)

        if len(recovered) < len(pending):
            raise MemoryGraphUnavailable(
                f"recovered {len(recovered)} of {len(pending)} pending episodes; "
                "graph may be unavailable"
            )
        return recovered
