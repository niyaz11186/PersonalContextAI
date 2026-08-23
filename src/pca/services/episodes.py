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
            # ERROR, not WARNING. A failed ingestion means this memory is invisible
            # to retrieval until recovery runs, and the user-visible symptom is the
            # assistant claiming to have no history — indistinguishable from
            # genuinely having none. That must not hide at warning level.
            _log.error(
                "episode_ingest_failed",
                episode_id=str(episode.id),
                error=str(exc)[:300],
                consequence="memory not searchable until recovery; /health reports the backlog",
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

    async def pending_count(self, limit: int = 500) -> int:
        """How many episodes are persisted but not in the graph.

        Surfaced through /health. Without this, a broken ingestion pipeline is
        invisible: the API keeps returning 200, replies look normal, and memory
        quietly accumulates nowhere. A non-zero backlog is the signal that
        retrieval is answering from less than it should.
        """
        return len(await self._repository.pending(limit))

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
            # Deliberately does NOT raise. An earlier version failed startup here,
            # which is the wrong trade: a recoverable backlog would leave the
            # application completely unusable, when it could run with reduced
            # memory and a visible backlog on /health. The episodes stay durable in
            # PostgreSQL and will be retried on the next start.
            _log.error(
                "pending_episode_recovery_incomplete",
                recovered=len(recovered),
                pending=len(pending),
                consequence="those memories are not searchable; see /health backlog",
            )
        return recovered
