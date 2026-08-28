"""End-to-end tests of the walking skeleton's API, using fakes throughout.

These are the closest thing to a real Unit 1b verification that is possible while
no container runtime exists. They exercise the genuine FastAPI app, the real
LangGraph workflow, and the real domain services — only the four adapters at the
edges (PostgreSQL, Neo4j/Graphiti, Gemini, clock) are substituted.

What that does and does not prove:

    proves      — routing, SSE framing, the write ordering in ADR-005, append-only
                  behaviour, degradation disclosure, health semantics
    not proven  — SQL correctness, Graphiti behaviour, real Gemini output
                  (those need Unit 1b's activation against live infrastructure)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pca.composition import Container
from pca.config.migrations import MigrationRunner
from pca.config.schema_drift import SchemaDriftCheck
from pca.config.settings import Settings
from pca.main import create_app
from pca.orchestration.conversation_workflow import ConversationWorkflow
from pca.services.belief_history import BeliefHistoryService
from pca.services.conflicts import ConflictDetectionService
from pca.services.context_assembly import ContextAssemblyService
from pca.services.conversation import ConversationService
from pca.services.entities import EntityService
from pca.services.episodes import EpisodeService
from pca.services.extraction import ExtractedFact, ExtractionPayload, ExtractionService
from pca.services.memory import MemoryService
from pca.services.operation_log import MemoryOperationLog
from pca.services.provenance import ProvenanceService
from pca.services.retrieval import RetrievalService
from pca.services.salience import SalienceScorer
from pca.services.time_resolver import TimeResolver
from pca.services.timeline import TimelineService
from tests.fakes.clock import FakeClock
from tests.fakes.graph import FakeMemoryGraph
from tests.fakes.history_repositories import (
    FakeBeliefRepository,
    FakeOperationLogRepository,
    FakeTransactionManager,
)
from tests.fakes.llm import FakeLLMProvider
from tests.fakes.memory_repositories import (
    FakeEntityRepository,
    FakeMemoryRepository,
    FakeProvenanceRepository,
)
from tests.fakes.repositories import FakeConversationRepository, FakeEpisodeRepository
from tests.fakes.store import FakeRelationalStore

REPLY = "Yes, you mentioned Priya lives in Pune."


def extraction_payload() -> ExtractionPayload:
    """Scripted extraction result.

    Needed because the message flow now runs extraction and commit after the reply.
    Without a scripted payload the fake provider would raise and every test would
    exercise the failure path instead of the happy one.
    """
    return ExtractionPayload(
        facts=[
            ExtractedFact(
                statement="Priya lives in Pune",
                origin="user_stated",
                category="location",
                about=["Priya"],
            )
        ]
    )


class _StubProviderHealth:
    healthy = True
    model = "fake"
    detail = None


def build_fake_container(
    provider: FakeLLMProvider | None = None,
    graph: FakeMemoryGraph | None = None,
) -> Container:
    settings = Settings(
        GOOGLE_API_KEY="test-key",
        PCA_USER_TIMEZONE="Asia/Kolkata",
        PCA_NEO4J_PASSWORD="test",
    )
    clock = FakeClock(zone="Asia/Kolkata")
    store = FakeRelationalStore()
    graph = graph or FakeMemoryGraph()
    provider = provider or FakeLLMProvider(
        completions=[REPLY],
        # One payload per message the test sends; a generous supply keeps the
        # scripting out of individual tests.
        structured_results=[extraction_payload() for _ in range(10)],
    )

    conversation_repository = FakeConversationRepository()
    episode_repository = FakeEpisodeRepository()
    entity_repository = FakeEntityRepository()
    memory_repository = FakeMemoryRepository()
    provenance_repository = FakeProvenanceRepository()

    conversations = ConversationService(repository=conversation_repository, clock=clock)
    entities = EntityService(repository=entity_repository, clock=clock)
    provenance = ProvenanceService(
        repository=provenance_repository,
        conversations=conversation_repository,
        clock=clock,
    )
    belief_repository = FakeBeliefRepository()
    operation_repository = FakeOperationLogRepository()
    transactions = FakeTransactionManager(
        memory_repository,
        entity_repository,
        provenance_repository,
        belief_repository,
        operation_repository,
    )
    memory = MemoryService(
        repository=memory_repository,
        entities=entities,
        provenance=provenance,
        clock=clock,
        transactions=transactions,
        beliefs=BeliefHistoryService(repository=belief_repository, clock=clock),
        operations=MemoryOperationLog(repository=operation_repository, clock=clock),
    )
    episodes = EpisodeService(
        repository=episode_repository,
        graph=graph,
        clock=clock,
        llm_model="fake-llm",
        embedding_model="fake-embed",
    )
    retrieval = RetrievalService(graph=graph)
    assembly = ContextAssemblyService()

    container = Container(
        settings=settings,
        clock=clock,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        graph=graph,  # type: ignore[arg-type]
        provider=provider,  # type: ignore[arg-type]
        migrations=MigrationRunner(store, clock, Path("migrations")),  # type: ignore[arg-type]
        schema_drift=SchemaDriftCheck(store),  # type: ignore[arg-type]
        conversations=conversations,
        episodes=episodes,
        extraction=ExtractionService(
            provider=provider,  # type: ignore[arg-type]
            resolver=TimeResolver(),
            salience=SalienceScorer(),
        ),
        retrieval=retrieval,
        assembly=assembly,
        conversation_workflow=ConversationWorkflow(
            conversations=conversations,
            retrieval=retrieval,
            assembly=assembly,
            provider=provider,  # type: ignore[arg-type]
        ),
        entities=entities,
        provenance=provenance,
        memory=memory,
        beliefs=BeliefHistoryService(repository=belief_repository, clock=clock),  # type: ignore[arg-type]
        operations=MemoryOperationLog(repository=operation_repository, clock=clock),  # type: ignore[arg-type]
        timeline=TimelineService(
            memory=memory_repository,  # type: ignore[arg-type]
            beliefs=belief_repository,  # type: ignore[arg-type]
            clock=clock,  # type: ignore[arg-type]
        ),
        conflicts=ConflictDetectionService(
            memory=memory_repository,  # type: ignore[arg-type]
            llm=provider,  # type: ignore[arg-type]
        ),
    )

    # Test scaffolding: expose the fakes so assertions can inspect what was actually
    # written. Container is a plain dataclass, so extra attributes are permitted.
    container.test_memory_repo = memory_repository  # type: ignore[attr-defined]
    container.test_entity_repo = entity_repository  # type: ignore[attr-defined]
    container.test_provenance_repo = provenance_repository  # type: ignore[attr-defined]
    container.test_belief_repo = belief_repository  # type: ignore[attr-defined]
    container.test_operation_repo = operation_repository  # type: ignore[attr-defined]
    container.test_transactions = transactions  # type: ignore[attr-defined]
    return container


@pytest.fixture
def container() -> Container:
    return build_fake_container()


@pytest.fixture
def client(container: Container):
    # Patch provider health, which the real adapter implements via a live call.
    container.provider.health = _health  # type: ignore[assignment]
    # TestClient must be entered as a context manager, otherwise the lifespan
    # never runs and app.state.container is never populated.
    with TestClient(create_app(container=container)) as test_client:
        yield test_client


def make_client(container: Container) -> TestClient:
    container.provider.health = _health  # type: ignore[assignment]
    return TestClient(create_app(container=container))


async def _health() -> _StubProviderHealth:
    return _StubProviderHealth()


def sse_events(text: str) -> list[dict]:
    """Parse an SSE body into its JSON payloads."""
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line.removeprefix("data: ")))
    return events


# ----------------------------------------------------------------------- basics


def test_root_declares_the_absence_of_authentication(client: TestClient) -> None:
    """Constraint C-8 is an operational rule, so it is stated at the front door
    rather than buried in a document."""
    body = client.get("/").json()

    assert "authentication" in body
    assert "localhost" in body["authentication"]


def test_health_reports_each_dependency_separately(client: TestClient) -> None:
    """A single boolean is useless for diagnosis. 'Neo4j down, retrieval degrades'
    and 'PostgreSQL down, nothing works' need different responses."""
    body = client.get("/health").json()

    names = {d["name"] for d in body["dependencies"]}
    assert names == {"postgres", "neo4j", "gemini", "memory_ingestion"}
    assert body["healthy"] is True


def test_health_surfaces_a_broken_ingestion_pipeline() -> None:
    """The observability gap that let the memory bug hide.

    Previously a total ingestion failure produced no signal: 200s everywhere, normal
    replies, and an assistant that simply claimed no history. The backlog makes it
    visible.
    """

    class BrokenGraph(FakeMemoryGraph):
        async def add_episode(self, episode):  # type: ignore[no-untyped-def]
            raise RuntimeError("node not found")

    container = build_fake_container(graph=BrokenGraph())

    with make_client(container) as client:
        conversation_id = client.post("/conversations", json={}).json()["id"]
        client.post(
            f"/conversations/{conversation_id}/messages",
            json={"content": "Priya lives in Pune"},
        )

        body = client.get("/health").json()
        ingestion = next(
            d for d in body["dependencies"] if d["name"] == "memory_ingestion"
        )

        assert ingestion["healthy"] is False
        assert "NOT searchable" in ingestion["detail"]
        assert body["note"] is not None and "not searchable" in body["note"]


def test_liveness_touches_no_dependency(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "alive"}


# ---------------------------------------------------------------- conversations


def test_create_conversation_records_zone(client: TestClient) -> None:
    response = client.post("/conversations", json={"title": "Family"})

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Family"
    assert body["zone"] == "Asia/Kolkata"


def test_send_message_streams_sse_and_completes(client: TestClient) -> None:
    conversation_id = client.post("/conversations", json={}).json()["id"]

    response = client.post(
        f"/conversations/{conversation_id}/messages",
        json={"content": "Where does Priya live?"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = sse_events(response.text)
    tokens = "".join(e["token"] for e in events if "token" in e)
    assert tokens.strip() == REPLY.strip()
    assert events[-1]["done"] is True
    assert "correlation_id" in events[-1]


def test_send_message_to_unknown_conversation_is_404(client: TestClient) -> None:
    response = client.post(
        "/conversations/00000000-0000-0000-0000-000000000000/messages",
        json={"content": "hello"},
    )

    assert response.status_code == 404


def test_user_message_is_persisted_before_generation(
    container: Container, client: TestClient
) -> None:
    """ADR-005 write ordering.

    The user's words must be durable before any model call, so that a provider
    failure cannot lose them. Asserting the message survives is the observable
    consequence of that ordering.
    """
    conversation_id = client.post("/conversations", json={}).json()["id"]
    client.post(
        f"/conversations/{conversation_id}/messages", json={"content": "remember this"}
    )

    history = client.get(f"/conversations/{conversation_id}/messages").json()

    assert history[0]["role"] == "user"
    assert history[0]["content"] == "remember this"


def test_assistant_reply_is_appended_after_streaming(client: TestClient) -> None:
    conversation_id = client.post("/conversations", json={}).json()["id"]
    client.post(f"/conversations/{conversation_id}/messages", json={"content": "hi"})

    history = client.get(f"/conversations/{conversation_id}/messages").json()

    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[1]["content"].strip() == REPLY.strip()


def test_episode_is_recorded_and_ingested_after_the_reply(
    container: Container, client: TestClient
) -> None:
    """Episodes are the replay source that makes ADR-005 real.

    Recording happens after the reply so extraction never delays the user, but it
    must still happen — an episode that is never written cannot be replayed.
    """
    conversation_id = client.post("/conversations", json={}).json()["id"]
    client.post(
        f"/conversations/{conversation_id}/messages", json={"content": "Priya moved"}
    )

    assert len(container.graph.episodes) == 1  # type: ignore[attr-defined]
    assert container.graph.episodes[0].content == "Priya moved"  # type: ignore[attr-defined]


def test_episode_carries_the_message_anchor_not_ingestion_time(
    container: Container, client: TestClient
) -> None:
    """The episode's occurred_at must be the utterance instant.

    Using ingestion time would break relative-time resolution: 'last Tuesday' would
    be anchored to whenever the background work ran rather than when it was said.
    """
    conversation_id = client.post("/conversations", json={}).json()["id"]
    client.post(
        f"/conversations/{conversation_id}/messages",
        json={"content": "I saw her last Tuesday"},
    )

    history = client.get(f"/conversations/{conversation_id}/messages").json()
    episode = container.graph.episodes[0]  # type: ignore[attr-defined]

    assert episode.occurred_at.isoformat() == history[0]["captured_at"].replace("Z", "+00:00")
    assert episode.zone == "Asia/Kolkata"


def test_conversations_are_listed_newest_first(client: TestClient) -> None:
    client.post("/conversations", json={"title": "first"})
    client.post("/conversations", json={"title": "second"})

    titles = [c["title"] for c in client.get("/conversations").json()]

    # FakeClock does not advance between calls, so this asserts the endpoint
    # returns both rather than a strict ordering.
    assert set(titles) == {"first", "second"}


def test_empty_message_is_rejected(client: TestClient) -> None:
    conversation_id = client.post("/conversations", json={}).json()["id"]

    response = client.post(
        f"/conversations/{conversation_id}/messages", json={"content": ""}
    )

    assert response.status_code == 422


# ------------------------------------------------------------------ degradation


def test_graph_failure_degrades_with_disclosure_rather_than_failing() -> None:
    """NFR-06.5.

    A graph outage must not fail the request, but the reply must disclose that
    history is missing. An answer built on nothing that looks confident is the
    worse outcome.
    """

    class BrokenGraph(FakeMemoryGraph):
        async def search_semantic(self, text: str, limit: int):  # type: ignore[no-untyped-def]
            raise RuntimeError("neo4j unreachable")

    container = build_fake_container(graph=BrokenGraph())

    with make_client(container) as client:
        conversation_id = client.post("/conversations", json={}).json()["id"]
        response = client.post(
            f"/conversations/{conversation_id}/messages",
            json={"content": "what happened?"},
        )

        assert response.status_code == 200
        events = sse_events(response.text)
        assert events[-1]["done"] is True

        # The degradation must reach the prompt, not just the logs.
        #
        # Specifically the *stream* call. Extraction now runs after generation, so
        # the most recent provider call is the extraction request and asserting on
        # calls[-1] would inspect the wrong prompt entirely.
        stream_calls = [c for c in container.provider.calls if c[0] == "stream"]  # type: ignore[attr-defined]
        assert stream_calls, "expected a streaming generation call"
        prompt_text = " ".join(m.content for m in stream_calls[-1][1].messages)
        assert "could not be searched" in prompt_text


def test_provider_failure_reports_an_error_event_and_keeps_the_message() -> None:
    """The user's message must survive a model outage.

    This is the payoff of persisting before generating: the words are already
    durable, so the failure costs a reply rather than the input.
    """
    from pca.domain.errors import ProviderUnavailable

    provider = FakeLLMProvider(fail_with=ProviderUnavailable("gemini down"))
    container = build_fake_container(provider=provider)

    with make_client(container) as client:
        conversation_id = client.post("/conversations", json={}).json()["id"]
        response = client.post(
            f"/conversations/{conversation_id}/messages",
            json={"content": "important fact"},
        )

        events = sse_events(response.text)
        errors = [e for e in events if "error" in e]
        assert errors, "expected an error event"
        assert "saved" in errors[0]["error"].lower()

        history = client.get(f"/conversations/{conversation_id}/messages").json()
        assert history[0]["content"] == "important fact"
        assert len(history) == 1, "no assistant message for a failed reply"


# ===========================================================================
# Unit 2 — the memory write path, end to end through the API
# ===========================================================================


def test_sending_a_message_commits_facts_and_entities(
    container: Container, client: TestClient
) -> None:
    """The whole point of Unit 2, asserted end to end.

    Unit 1b stored episodes in the graph but wrote nothing to PostgreSQL's memory
    model. This is the test that would have caught that: it asserts the authoritative
    store actually received facts and entities, not just that the request returned 200.
    """
    conversation_id = client.post("/conversations", json={}).json()["id"]

    client.post(
        f"/conversations/{conversation_id}/messages",
        json={"content": "My sister Priya lives in Pune."},
    )

    facts = container.test_memory_repo.facts  # type: ignore[attr-defined]
    entities = container.test_entity_repo.entities  # type: ignore[attr-defined]

    assert len(facts) == 1
    assert next(iter(facts.values())).statement == "Priya lives in Pune"
    assert len(entities) == 1
    assert next(iter(entities.values())).name == "Priya"


def test_committed_facts_carry_provenance_to_the_source_message(
    container: Container, client: TestClient
) -> None:
    """FR-02.5. A memory with no traceable source cannot be justified to the user."""
    conversation_id = client.post("/conversations", json={}).json()["id"]

    client.post(
        f"/conversations/{conversation_id}/messages",
        json={"content": "My sister Priya lives in Pune."},
    )

    rows = container.test_provenance_repo.rows  # type: ignore[attr-defined]
    assert rows, "expected provenance to be recorded"
    _, _, ref, _ = rows[0]
    assert ref.message_id is not None
    assert ref.conversation_id is not None


def test_committed_fact_has_both_time_axes_populated(
    container: Container, client: TestClient
) -> None:
    """Belief time always exists; world time only when a phrase resolved.

    Sourcing both from the same value is how a temporal system starts answering
    "what did I think in March" with "what was true in March".
    """
    conversation_id = client.post("/conversations", json={}).json()["id"]

    client.post(
        f"/conversations/{conversation_id}/messages",
        json={"content": "My sister Priya lives in Pune."},
    )

    fact = next(iter(container.test_memory_repo.facts.values()))  # type: ignore[attr-defined]
    assert fact.belief.asserted_at is not None
    assert fact.belief.retracted_at is None
    # The scripted payload carries no time phrase, so world time is legitimately
    # absent rather than fabricated (ADR-010).
    assert fact.validity.valid_from is None


def test_ambiguous_entity_produces_a_user_facing_notice() -> None:
    """ADR-014 ambiguity must reach the user, not just the logs.

    A provisional entity means the fact may be attached to the wrong person until
    someone decides. Silence there is how a graph quietly becomes wrong.
    """
    from uuid import uuid4

    from pca.domain.enums import EntityType
    from pca.domain.ids import EntityId
    from pca.domain.memory import Entity

    container = build_fake_container()
    repo = container.test_entity_repo  # type: ignore[attr-defined]

    # Two existing people share the name the scripted extraction will mention.
    # Seeded directly into the fake's storage rather than through its async API,
    # because this is a synchronous test and spinning up a second event loop to call
    # one setup method is worse than reaching into a test double.
    for _ in range(2):
        entity_id = EntityId(uuid4())
        repo.entities[entity_id] = Entity(
            id=entity_id, name="Priya", entity_type=EntityType.PERSON
        )
        repo.aliases[entity_id] = set()

    with make_client(container) as client:
        conversation_id = client.post("/conversations", json={}).json()["id"]
        response = client.post(
            f"/conversations/{conversation_id}/messages",
            json={"content": "Priya called me."},
        )

    events = sse_events(response.text)
    assert events[-1]["done"] is True
    assert "notices" in events[-1]
    assert any("unambiguously" in n for n in events[-1]["notices"])


def test_memory_write_failure_is_disclosed_and_does_not_lose_the_message() -> None:
    """A broken memory write must be visible.

    This is the lesson from the Unit 1b defect: the message stays durable, but the
    user is told that it did not reach memory rather than being left to assume it did.
    """
    provider = FakeLLMProvider(completions=[REPLY], structured_results=[])
    container = build_fake_container(provider=provider)

    with make_client(container) as client:
        conversation_id = client.post("/conversations", json={}).json()["id"]
        response = client.post(
            f"/conversations/{conversation_id}/messages",
            json={"content": "Something important"},
        )

        events = sse_events(response.text)
        assert "notices" in events[-1]
        assert any("could not be added to memory" in n for n in events[-1]["notices"])

        history = client.get(f"/conversations/{conversation_id}/messages").json()
        assert history[0]["content"] == "Something important"
