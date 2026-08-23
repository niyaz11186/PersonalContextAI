"""Tests for EpisodeService — the ADR-005 write path.

The ordering guarantee here is what makes "Neo4j is disposable" true rather than
aspirational: persist to PostgreSQL first, ingest second, mark the watermark third.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from pca.domain.conversation import Message
from pca.domain.enums import Role
from pca.domain.ids import ConversationId, MessageId
from pca.services.episodes import EpisodeService
from tests.fakes.clock import FakeClock
from tests.fakes.graph import FakeMemoryGraph
from tests.fakes.repositories import FakeEpisodeRepository

CAPTURED = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)


def make_message(content: str = "Priya lives in Pune") -> Message:
    return Message(
        id=MessageId(uuid4()),
        conversation_id=ConversationId(uuid4()),
        role=Role.USER,
        content=content,
        captured_at=CAPTURED,
        zone="Asia/Kolkata",
    )


def make_service(
    graph: FakeMemoryGraph | None = None,
    repository: FakeEpisodeRepository | None = None,
) -> tuple[EpisodeService, FakeEpisodeRepository, FakeMemoryGraph]:
    repository = repository or FakeEpisodeRepository()
    graph = graph or FakeMemoryGraph()
    service = EpisodeService(
        repository=repository,
        graph=graph,
        clock=FakeClock(start=datetime(2026, 6, 1, tzinfo=UTC), zone="Asia/Kolkata"),
        llm_model="gemini-3.5-flash",
        embedding_model="gemini-embedding-001",
    )
    return service, repository, graph


async def test_episode_copies_the_message_anchor_not_clock_time() -> None:
    """Extraction may run long after the message.

    Anchoring to clock time would resolve "last Tuesday" against whenever the
    background work happened rather than when it was said.
    """
    service, _, _ = make_service()
    message = make_message()

    episode = await service.record_message(message)

    assert episode.occurred_at == CAPTURED
    assert episode.zone == "Asia/Kolkata"
    assert episode.message_id == message.id


async def test_model_identifiers_are_recorded_with_the_episode() -> None:
    """Embeddings from different models are not comparable.

    Without recording which model produced them, a model change silently degrades
    retrieval with no way to detect the mismatch (ADR-013).
    """
    service, repository, _ = make_service()

    episode = await service.record_message(make_message())

    assert repository.models[episode.id] == (
        "gemini-3.5-flash",
        "gemini-embedding-001",
    )


async def test_persist_happens_before_ingestion() -> None:
    """The durability point is the PostgreSQL write.

    If the graph write failed first and PostgreSQL second, memory would exist that
    cannot be rebuilt — and ADR-005's central safety property would be gone.
    """
    service, repository, graph = make_service()

    episode = await service.record_message(make_message())

    assert await repository.get(episode.id) is not None
    assert graph.episodes == []  # not yet ingested

    await service.ingest(episode)
    assert len(graph.episodes) == 1


async def test_watermark_is_set_only_after_successful_ingestion() -> None:
    service, repository, _ = make_service()
    episode = await service.record_message(make_message())

    assert (await repository.get(episode.id)).ingested_at is None

    await service.ingest(episode)

    assert (await repository.get(episode.id)).ingested_at is not None


async def test_ingest_failure_returns_false_and_leaves_the_episode_pending() -> None:
    """The exact shape of the defect that broke the pipeline.

    A failed ingestion must not raise — the episode is already durable, so this is
    retryable. But it must remain pending so recovery and /health can see it.
    """

    class BrokenGraph(FakeMemoryGraph):
        async def add_episode(self, episode):  # type: ignore[no-untyped-def]
            raise RuntimeError("node not found")

    service, repository, _ = make_service(graph=BrokenGraph())
    episode = await service.record_message(make_message())

    assert await service.ingest(episode) is False
    assert (await repository.get(episode.id)).ingested_at is None
    assert await service.pending_count() == 1


async def test_pending_count_reports_the_backlog() -> None:
    """This is what makes a broken pipeline visible on /health.

    Previously a total ingestion failure produced no observable signal at all.
    """

    class BrokenGraph(FakeMemoryGraph):
        async def add_episode(self, episode):  # type: ignore[no-untyped-def]
            raise RuntimeError("down")

    service, _, _ = make_service(graph=BrokenGraph())

    for _ in range(3):
        await service.record_and_ingest(make_message())

    assert await service.pending_count() == 3


async def test_pending_count_is_zero_when_healthy() -> None:
    service, _, _ = make_service()

    await service.record_and_ingest(make_message())

    assert await service.pending_count() == 0


async def test_recover_pending_reingests_stuck_episodes() -> None:
    """Recovery is what lets a transient graph outage self-heal on restart."""
    repository = FakeEpisodeRepository()

    class BrokenGraph(FakeMemoryGraph):
        async def add_episode(self, episode):  # type: ignore[no-untyped-def]
            raise RuntimeError("down")

    broken, _, _ = make_service(graph=BrokenGraph(), repository=repository)
    await broken.record_and_ingest(make_message("first"))
    await broken.record_and_ingest(make_message("second"))
    assert await broken.pending_count() == 2

    healthy, _, graph = make_service(repository=repository)
    recovered = await healthy.recover_pending()

    assert len(recovered) == 2
    assert len(graph.episodes) == 2
    assert await healthy.pending_count() == 0


async def test_partial_recovery_does_not_raise() -> None:
    """Startup must not be blocked by a recoverable backlog.

    An earlier version raised here, which would leave the application completely
    unusable when it could run with reduced memory and a visible backlog instead.
    """

    class FlakyGraph(FakeMemoryGraph):
        def __init__(self) -> None:
            super().__init__()
            self._calls = 0

        async def add_episode(self, episode):  # type: ignore[no-untyped-def]
            self._calls += 1
            if self._calls == 1:
                return await super().add_episode(episode)
            raise RuntimeError("down again")

    repository = FakeEpisodeRepository()
    broken, _, _ = make_service(graph=type("G", (FakeMemoryGraph,), {
        "add_episode": lambda self, e: (_ for _ in ()).throw(RuntimeError("down"))
    })(), repository=repository)
    await broken.record_and_ingest(make_message("a"))
    await broken.record_and_ingest(make_message("b"))

    flaky, _, _ = make_service(graph=FlakyGraph(), repository=repository)
    recovered = await flaky.recover_pending()  # must not raise

    assert len(recovered) == 1
    assert await flaky.pending_count() == 1
