"""ExtractionWorkflow — the background write path moved off the response.

Extraction and conflict detection are stubbed here; both have their own suites. What
these tests exercise is the orchestration: node order, the two short circuits, and
the conflict branch from `services.md` Workflow 2. `MemoryService` is the real thing
over fake repositories, because "did anything actually get written twice" is the
question the idempotency tests need answered honestly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from pca.domain.conversation import Episode
from pca.domain.enums import Confidence, ConflictKind, MemoryKind, Origin
from pca.domain.extraction import CandidateFact, ExtractionCandidates
from pca.domain.ids import ConversationId, EpisodeId, MessageId
from pca.domain.memory import Conflict
from pca.orchestration.extraction_workflow import ExtractionWorkflow
from pca.services.belief_history import BeliefHistoryService
from pca.services.entities import EntityService
from pca.services.episodes import EpisodeService
from pca.services.memory import MemoryService
from pca.services.operation_log import MemoryOperationLog
from pca.services.provenance import ProvenanceService
from tests.fakes.clock import FakeClock
from tests.fakes.graph import FakeMemoryGraph
from tests.fakes.history_repositories import (
    FakeBeliefRepository,
    FakeOperationLogRepository,
    FakeTransactionManager,
)
from tests.fakes.memory_repositories import (
    FakeEntityRepository,
    FakeMemoryRepository,
    FakeProvenanceRepository,
)
from tests.fakes.repositories import FakeConversationRepository, FakeEpisodeRepository

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
UTTERED = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


# --------------------------------------------------------------------- stubs


@dataclass
class StubExtraction:
    candidates: ExtractionCandidates | None = None
    calls: int = 0

    async def extract(self, episode: Episode) -> ExtractionCandidates:
        self.calls += 1
        return self.candidates or ExtractionCandidates(episode_id=episode.id)


@dataclass
class StubConflicts:
    detected: list[Conflict] | None = None
    calls: int = 0

    async def detect(self, candidates: ExtractionCandidates) -> list[Conflict]:
        self.calls += 1
        return list(self.detected or [])

    def supersessions(self, conflicts) -> list[Conflict]:
        return [c for c in conflicts if c.kind is ConflictKind.TEMPORAL_CHANGE]

    def contradictions(self, conflicts) -> list[Conflict]:
        return [c for c in conflicts if c.kind is ConflictKind.CONTRADICTION]


# ------------------------------------------------------------------ assembly


class Harness:
    def __init__(self) -> None:
        self.clock = FakeClock(start=NOW, zone="Asia/Kolkata")
        self.episode_repo = FakeEpisodeRepository()
        self.memory_repo = FakeMemoryRepository()
        self.entity_repo = FakeEntityRepository()
        self.provenance_repo = FakeProvenanceRepository()
        self.graph = FakeMemoryGraph()

        belief_repo = FakeBeliefRepository()
        operation_repo = FakeOperationLogRepository()

        self.provenance = ProvenanceService(
            repository=self.provenance_repo,
            conversations=FakeConversationRepository(),
            clock=self.clock,
        )
        self.memory = MemoryService(
            repository=self.memory_repo,
            entities=EntityService(repository=self.entity_repo, clock=self.clock),
            provenance=self.provenance,
            clock=self.clock,
            transactions=FakeTransactionManager(
                self.memory_repo,
                self.entity_repo,
                self.provenance_repo,
                belief_repo,
                operation_repo,
            ),
            beliefs=BeliefHistoryService(repository=belief_repo, clock=self.clock),
            operations=MemoryOperationLog(repository=operation_repo, clock=self.clock),
        )
        self.episodes = EpisodeService(
            repository=self.episode_repo,
            graph=self.graph,
            clock=self.clock,
            llm_model="m",
            embedding_model="e",
        )
        self.extraction = StubExtraction()
        self.conflicts = StubConflicts()

    def workflow(self) -> ExtractionWorkflow:
        return ExtractionWorkflow(
            episodes=self.episodes,
            episode_repository=self.episode_repo,
            extraction=self.extraction,  # type: ignore[arg-type]
            conflicts=self.conflicts,  # type: ignore[arg-type]
            memory=self.memory,
            provenance=self.provenance,
        )

    async def given_episode(self, content: str = "Priya lives in Pune.") -> Episode:
        episode = Episode(
            id=EpisodeId(uuid4()),
            content=content,
            occurred_at=UTTERED,
            zone="Asia/Kolkata",
            conversation_id=ConversationId(uuid4()),
            message_id=MessageId(uuid4()),
        )
        await self.episode_repo.save(episode, llm_model="m", embedding_model="e")
        return episode


def _fact(statement: str = "Priya lives in Pune") -> CandidateFact:
    return CandidateFact(
        statement=statement,
        origin=Origin.USER_STATED,
        confidence=Confidence.CERTAIN,
        subject_names=["Priya"],
    )


@pytest.fixture
def harness() -> Harness:
    return Harness()


# ------------------------------------------------------------------ happy path


async def test_it_extracts_detects_and_commits(harness: Harness) -> None:
    episode = await harness.given_episode()
    harness.extraction.candidates = ExtractionCandidates(
        episode_id=episode.id, facts=[_fact()]
    )

    outcome = await harness.workflow().run(episode.id)

    assert outcome.facts_committed == 1
    assert not outcome.already_done
    assert len(harness.memory_repo.facts) == 1


async def test_conflict_detection_runs_before_the_commit(harness: Harness) -> None:
    """Position matters, not merely presence.

    Detecting afterwards would mean the store already held both versions with no
    record that they disagree — the state FR-05 exists to prevent.
    """
    episode = await harness.given_episode()
    seen_facts_at_detection: list[int] = []

    async def detect(candidates: ExtractionCandidates) -> list[Conflict]:
        seen_facts_at_detection.append(len(harness.memory_repo.facts))
        return []

    harness.extraction.candidates = ExtractionCandidates(
        episode_id=episode.id, facts=[_fact()]
    )
    harness.conflicts.detect = detect  # type: ignore[method-assign]

    await harness.workflow().run(episode.id)

    assert seen_facts_at_detection == [0], "commit happened before detection"
    assert len(harness.memory_repo.facts) == 1


# ---------------------------------------------------------------- idempotency


async def test_an_already_committed_episode_is_not_written_twice(
    harness: Harness,
) -> None:
    """The dangerous crash window ADR-008's primary key does NOT cover.

    `extraction_status.episode_id` prevents duplicate *submits*. It does nothing
    about a crash between `commit` and marking the row finished — recovery re-runs
    that episode, and without this check every fact is written a second time.
    """
    episode = await harness.given_episode()
    harness.extraction.candidates = ExtractionCandidates(
        episode_id=episode.id, facts=[_fact()]
    )
    workflow = harness.workflow()

    first = await workflow.run(episode.id)
    second = await workflow.run(episode.id)

    assert first.facts_committed == 1
    assert second.already_done
    assert second.facts_committed == 0
    assert len(harness.memory_repo.facts) == 1, "the episode was committed twice"
    assert harness.extraction.calls == 1, "re-extracted an episode already committed"


# ------------------------------------------------------------- short circuits


async def test_an_empty_extraction_commits_nothing_and_costs_no_detection(
    harness: Harness,
) -> None:
    """Not a failure — some messages carry nothing worth remembering.

    But running detection and a commit over an empty set spends a model call and a
    transaction to write nothing.
    """
    episode = await harness.given_episode("Morning!")

    outcome = await harness.workflow().run(episode.id)

    assert outcome.facts_committed == 0
    assert not outcome.already_done
    assert harness.conflicts.calls == 0
    assert harness.memory_repo.facts == {}


async def test_a_missing_episode_raises(harness: Harness) -> None:
    """The coordinator records this as FAILED rather than swallowing it.

    Silently returning an empty outcome would mark the extraction SUCCEEDED and the
    episode would never be retried.
    """
    with pytest.raises(LookupError):
        await harness.workflow().run(EpisodeId(uuid4()))


# ------------------------------------------------------------------ ADR-005


async def test_a_graph_failure_does_not_stop_the_commit(harness: Harness) -> None:
    """The graph is a rebuildable projection; PostgreSQL is the record.

    Refusing to commit because Graphiti was unreachable would lose the memory
    permanently to a condition that resolves itself on the next reindex.
    """
    episode = await harness.given_episode()
    harness.extraction.candidates = ExtractionCandidates(
        episode_id=episode.id, facts=[_fact()]
    )
    harness.graph.fail_strategies.add("add_episode")

    outcome = await harness.workflow().run(episode.id)

    assert "add_episode" in harness.graph.calls, "ingestion was never attempted"
    assert harness.graph.episodes == [], "the fake accepted an episode it should have refused"
    assert outcome.facts_committed == 1


# ------------------------------------------------------------ conflict branch


async def test_a_temporal_change_supersedes_the_earlier_fact(
    harness: Harness,
) -> None:
    """FR-04.4: "she moved in March" bounds the old fact, it does not falsify it."""
    episode = await harness.given_episode()
    harness.extraction.candidates = ExtractionCandidates(
        episode_id=episode.id, facts=[_fact("Priya lives in Bangalore")]
    )
    await harness.workflow().run(episode.id)
    original_id = next(iter(harness.memory_repo.facts))

    later = await harness.given_episode("She moved to Pune in March.")
    harness.extraction.candidates = ExtractionCandidates(
        episode_id=later.id, facts=[_fact("Priya lives in Pune")]
    )
    harness.conflicts.detected = [
        Conflict(
            kind=ConflictKind.TEMPORAL_CHANGE,
            incoming_statement="Priya lives in Pune",
            existing_memory_id=original_id,
            explanation="she moved",
        )
    ]

    await harness.workflow().run(later.id)

    original = await harness.memory_repo.get_fact(original_id)
    assert original is not None
    assert original.validity.valid_to is not None, "old state was not bounded"
    # Belief untouched: we still think it was true for its window.
    assert original.belief.retracted_at is None


async def test_a_failed_supersession_does_not_fail_the_extraction(
    harness: Harness,
) -> None:
    """The new fact is already committed.

    A failed supersession leaves the old one unbounded rather than losing anything,
    so aborting here would trade a small inaccuracy for a lost memory.
    """
    episode = await harness.given_episode()
    harness.extraction.candidates = ExtractionCandidates(
        episode_id=episode.id, facts=[_fact()]
    )
    harness.conflicts.detected = [
        Conflict(
            kind=ConflictKind.TEMPORAL_CHANGE,
            incoming_statement="whatever",
            # No such memory — supersede raises MemoryNotFound.
            existing_memory_id=uuid4(),  # type: ignore[arg-type]
            explanation="boom",
        )
    ]

    outcome = await harness.workflow().run(episode.id)

    assert outcome.facts_committed == 1


async def test_a_contradiction_is_surfaced_and_both_versions_kept(
    harness: Harness,
) -> None:
    """FR-05.6: surface, never resolve. There is deliberately no winner."""
    episode = await harness.given_episode()
    harness.extraction.candidates = ExtractionCandidates(
        episode_id=episode.id, facts=[_fact("Priya lives in Delhi")]
    )
    harness.conflicts.detected = [
        Conflict(
            kind=ConflictKind.CONTRADICTION,
            incoming_statement="Priya lives in Delhi",
            existing_memory_id=uuid4(),  # type: ignore[arg-type]
            explanation="this disagrees with an earlier record",
        )
    ]

    outcome = await harness.workflow().run(episode.id)

    assert outcome.contradictions == ["this disagrees with an earlier record"]
    assert outcome.facts_committed == 1, "a contradiction must not block the write"


async def test_provenance_links_the_memory_back_to_its_episode(
    harness: Harness,
) -> None:
    """FR-02.5, and the mechanism the idempotency check above depends on."""
    episode = await harness.given_episode()
    harness.extraction.candidates = ExtractionCandidates(
        episode_id=episode.id, facts=[_fact()]
    )

    await harness.workflow().run(episode.id)

    linked = await harness.provenance.memories_from_episode(episode.id)
    assert [kind for _, kind in linked] == [MemoryKind.FACT]
