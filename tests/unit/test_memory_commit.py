"""Tests for MemoryService.commit — the Unit 2 write path.

Focus is on the properties that would be expensive or impossible to repair later:

  - provenance on every record (FR-02.5), stored many-to-many for ADR-012
  - the two time axes populated independently (world time vs belief time)
  - entity resolution feeding fact subjects, without silent merging
  - origin preserved exactly (FR-02.7)
  - ambiguity surfaced in the receipt rather than swallowed
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from pca.domain.conversation import Episode
from pca.domain.enums import (
    Confidence,
    EntityType,
    Granularity,
    MemoryKind,
    Origin,
    ResolutionMethod,
    SalienceCategory,
)
from pca.domain.extraction import (
    CandidateEntity,
    CandidateEvent,
    CandidateFact,
    CandidateRelationship,
    ExtractionCandidates,
)
from pca.domain.ids import ConversationId, EntityId, EpisodeId, MessageId
from pca.domain.temporal import TemporalExpression
from pca.services.belief_history import BeliefHistoryService
from pca.services.entities import EntityService
from pca.services.memory import MemoryService
from pca.services.operation_log import MemoryOperationLog
from pca.services.provenance import ProvenanceService
from tests.fakes.clock import FakeClock
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
from tests.fakes.repositories import FakeConversationRepository

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
UTTERED = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW, zone="Asia/Kolkata")


@pytest.fixture
def memory_repo() -> FakeMemoryRepository:
    return FakeMemoryRepository()


@pytest.fixture
def entity_repo() -> FakeEntityRepository:
    return FakeEntityRepository()


@pytest.fixture
def provenance_repo() -> FakeProvenanceRepository:
    return FakeProvenanceRepository()


@pytest.fixture
def belief_repo() -> FakeBeliefRepository:
    return FakeBeliefRepository()


@pytest.fixture
def operation_repo() -> FakeOperationLogRepository:
    return FakeOperationLogRepository()


@pytest.fixture
def transactions(
    memory_repo: FakeMemoryRepository,
    entity_repo: FakeEntityRepository,
    provenance_repo: FakeProvenanceRepository,
    belief_repo: FakeBeliefRepository,
    operation_repo: FakeOperationLogRepository,
) -> FakeTransactionManager:
    return FakeTransactionManager(
        memory_repo, entity_repo, provenance_repo, belief_repo, operation_repo
    )


@pytest.fixture
def service(
    clock: FakeClock,
    memory_repo: FakeMemoryRepository,
    entity_repo: FakeEntityRepository,
    provenance_repo: FakeProvenanceRepository,
    belief_repo: FakeBeliefRepository,
    operation_repo: FakeOperationLogRepository,
    transactions: FakeTransactionManager,
) -> MemoryService:
    return MemoryService(
        repository=memory_repo,
        entities=EntityService(repository=entity_repo, clock=clock),
        provenance=ProvenanceService(
            repository=provenance_repo,
            conversations=FakeConversationRepository(),
            clock=clock,
        ),
        clock=clock,
        transactions=transactions,
        beliefs=BeliefHistoryService(repository=belief_repo, clock=clock),
        operations=MemoryOperationLog(repository=operation_repo, clock=clock),
    )


def episode() -> Episode:
    return Episode(
        id=EpisodeId(uuid4()),
        content="My friend Suresh is a frontend developer in Visakhapatnam.",
        occurred_at=UTTERED,
        zone="Asia/Kolkata",
        conversation_id=ConversationId(uuid4()),
        message_id=MessageId(uuid4()),
    )


def anchored_expression() -> TemporalExpression:
    return TemporalExpression(
        raw_phrase="last Tuesday",
        granularity=Granularity.DAY,
        method=ResolutionMethod.CLOCK_RELATIVE,
        anchor_zone="Asia/Kolkata",
        resolved_from=datetime(2026, 2, 23, 18, 30, tzinfo=UTC),
        resolved_to=datetime(2026, 2, 24, 18, 30, tzinfo=UTC),
    )


# ------------------------------------------------------------------- basic path


async def test_empty_candidates_write_nothing(
    service: MemoryService, memory_repo: FakeMemoryRepository
) -> None:
    receipt = await service.commit(
        ExtractionCandidates(episode_id=EpisodeId(uuid4())), episode()
    )

    assert receipt.total == 0
    assert memory_repo.facts == {}


async def test_commit_persists_facts_and_returns_a_receipt(
    service: MemoryService, memory_repo: FakeMemoryRepository
) -> None:
    """A receipt exists because the Unit 1b defect was a commit that silently wrote
    nothing. Returning what was written makes that observable."""
    source = episode()
    candidates = ExtractionCandidates(
        episode_id=source.id,
        facts=[
            CandidateFact(
                statement="Suresh is a frontend developer",
                origin=Origin.USER_STATED,
                salience=0.8,
                salience_category=SalienceCategory.IDENTITY,
                subject_names=["Suresh"],
            )
        ],
        entities=[CandidateEntity(name="Suresh", entity_type=EntityType.PERSON)],
    )

    receipt = await service.commit(candidates, source)

    assert len(receipt.fact_ids) == 1
    assert receipt.episode_id == source.id
    stored = memory_repo.facts[receipt.fact_ids[0]]
    assert stored.statement == "Suresh is a frontend developer"
    assert stored.salience == 0.8


# -------------------------------------------------------------------- the axes


async def test_belief_time_comes_from_the_clock_world_time_from_the_phrase(
    service: MemoryService, memory_repo: FakeMemoryRepository
) -> None:
    """The two axes must be populated from different sources.

    Belief time is when the system learned it (now). World time is when it was true,
    derived from the resolved phrase. Sourcing both from the same value is how a
    temporal system starts answering "what did I think in March" with "what was true
    in March".
    """
    candidates = ExtractionCandidates(
        episode_id=EpisodeId(uuid4()),
        facts=[
            CandidateFact(
                statement="Suresh moved to Visakhapatnam",
                origin=Origin.USER_STATED,
                temporal_expression=anchored_expression(),
            )
        ],
    )

    receipt = await service.commit(candidates, episode())
    fact = memory_repo.facts[receipt.fact_ids[0]]

    assert fact.belief.asserted_at == NOW
    assert fact.belief.retracted_at is None
    assert fact.validity.valid_from == datetime(2026, 2, 23, 18, 30, tzinfo=UTC)
    assert fact.belief.asserted_at != fact.validity.valid_from


async def test_unresolved_time_never_produces_a_world_date(
    service: MemoryService, memory_repo: FakeMemoryRepository
) -> None:
    """ADR-010: a fabricated date is indistinguishable from a real one once stored."""
    candidates = ExtractionCandidates(
        episode_id=EpisodeId(uuid4()),
        facts=[
            CandidateFact(
                statement="They stopped speaking before the wedding",
                origin=Origin.USER_STATED,
                temporal_expression=TemporalExpression(
                    raw_phrase="before the wedding",
                    granularity=Granularity.UNKNOWN,
                    method=ResolutionMethod.UNRESOLVED,
                    anchor_zone="Asia/Kolkata",
                ),
            )
        ],
    )

    receipt = await service.commit(candidates, episode())
    fact = memory_repo.facts[receipt.fact_ids[0]]

    assert fact.validity.valid_from is None
    assert fact.temporal_expression.raw_phrase == "before the wedding"


async def test_raw_phrase_survives_the_commit(
    service: MemoryService, memory_repo: FakeMemoryRepository
) -> None:
    candidates = ExtractionCandidates(
        episode_id=EpisodeId(uuid4()),
        facts=[
            CandidateFact(
                statement="Saw him",
                origin=Origin.USER_STATED,
                temporal_expression=anchored_expression(),
            )
        ],
    )

    receipt = await service.commit(candidates, episode())

    assert memory_repo.facts[receipt.fact_ids[0]].temporal_expression.raw_phrase == (
        "last Tuesday"
    )


# ----------------------------------------------------------------- provenance


async def test_every_fact_records_provenance(
    service: MemoryService, provenance_repo: FakeProvenanceRepository
) -> None:
    """FR-02.5. A memory that cannot be traced to a source cannot be justified to
    the user or corroborated under ADR-012."""
    source = episode()
    candidates = ExtractionCandidates(
        episode_id=source.id,
        facts=[
            CandidateFact(statement="A", origin=Origin.USER_STATED),
            CandidateFact(statement="B", origin=Origin.USER_STATED),
        ],
    )

    receipt = await service.commit(candidates, source)

    for fact_id in receipt.fact_ids:
        refs = await provenance_repo.for_memory(fact_id, MemoryKind.FACT)
        assert len(refs) == 1
        assert refs[0].episode_id == source.id
        assert refs[0].message_id == source.message_id


async def test_provenance_count_supports_the_corroboration_rule(
    service: MemoryService, provenance_repo: FakeProvenanceRepository
) -> None:
    """ADR-012 needs to count remaining sources before retracting a fact."""
    source = episode()
    candidates = ExtractionCandidates(
        episode_id=source.id,
        facts=[CandidateFact(statement="A", origin=Origin.USER_STATED)],
    )
    receipt = await service.commit(candidates, source)
    fact_id = receipt.fact_ids[0]

    assert await provenance_repo.count_for_memory(fact_id, MemoryKind.FACT) == 1


async def test_recording_the_same_source_twice_does_not_inflate_the_count(
    provenance_repo: FakeProvenanceRepository, clock: FakeClock
) -> None:
    """Otherwise a retried commit would make a single-source fact look corroborated,
    and source deletion would then wrongly leave it standing."""
    from pca.domain.ids import MemoryId
    from pca.domain.memory import ProvenanceRef

    memory_id = MemoryId(uuid4())
    ref = ProvenanceRef(episode_id=EpisodeId(uuid4()))

    for _ in range(3):
        await provenance_repo.record(memory_id, MemoryKind.FACT, ref, clock.now())

    assert await provenance_repo.count_for_memory(memory_id, MemoryKind.FACT) == 1


# -------------------------------------------------------------------- entities


async def test_fact_subjects_are_linked_to_resolved_entities(
    service: MemoryService, memory_repo: FakeMemoryRepository
) -> None:
    candidates = ExtractionCandidates(
        episode_id=EpisodeId(uuid4()),
        facts=[
            CandidateFact(
                statement="Suresh lives in Visakhapatnam",
                origin=Origin.USER_STATED,
                subject_names=["Suresh"],
            )
        ],
        entities=[CandidateEntity(name="Suresh", entity_type=EntityType.PERSON)],
    )

    receipt = await service.commit(candidates, episode())
    fact = memory_repo.facts[receipt.fact_ids[0]]

    assert len(fact.subject_entity_ids) == 1
    assert fact.subject_entity_ids[0] in receipt.entity_ids


async def test_entities_referenced_only_by_a_fact_are_still_resolved(
    service: MemoryService,
) -> None:
    """The model does not always list an entity it then references.

    Without collecting names from fact subjects too, the fact would lose its link to
    the person it is about.
    """
    candidates = ExtractionCandidates(
        episode_id=EpisodeId(uuid4()),
        facts=[
            CandidateFact(
                statement="Priya works at Google",
                origin=Origin.USER_STATED,
                subject_names=["Priya"],
            )
        ],
        entities=[],  # deliberately omitted
    )

    receipt = await service.commit(candidates, episode())

    assert len(receipt.entity_ids) == 1


async def test_repeated_names_across_facts_resolve_to_one_entity(
    service: MemoryService,
) -> None:
    candidates = ExtractionCandidates(
        episode_id=EpisodeId(uuid4()),
        facts=[
            CandidateFact(
                statement="Suresh is a developer",
                origin=Origin.USER_STATED,
                subject_names=["Suresh"],
            ),
            CandidateFact(
                statement="Suresh lives in Visakhapatnam",
                origin=Origin.USER_STATED,
                subject_names=["Suresh"],
            ),
        ],
    )

    receipt = await service.commit(candidates, episode())

    assert len(receipt.entity_ids) == 1


async def test_ambiguous_entity_is_reported_in_the_receipt(
    service: MemoryService, entity_repo: FakeEntityRepository, clock: FakeClock
) -> None:
    """ADR-014 ambiguity must reach the caller.

    A provisional entity means a fact may be attached to the wrong person until a
    human decides. Swallowing that would recreate the silent-failure class of bug.
    """
    for _ in range(2):
        await entity_repo.create(
            entity_id=EntityId(uuid4()),
            name="Sarah",
            entity_type=EntityType.PERSON,
            created_at=clock.now(),
        )

    candidates = ExtractionCandidates(
        episode_id=EpisodeId(uuid4()),
        facts=[
            CandidateFact(
                statement="Sarah called",
                origin=Origin.USER_STATED,
                subject_names=["Sarah"],
            )
        ],
    )

    receipt = await service.commit(candidates, episode())

    assert receipt.needs_clarification
    assert len(receipt.provisional_entity_ids) == 1


# --------------------------------------------------------------------- origin


async def test_origin_is_preserved_through_the_commit(
    service: MemoryService, memory_repo: FakeMemoryRepository
) -> None:
    """FR-02.7: an inference must never be stored as a user statement."""
    candidates = ExtractionCandidates(
        episode_id=EpisodeId(uuid4()),
        facts=[
            CandidateFact(statement="Stated", origin=Origin.USER_STATED),
            CandidateFact(
                statement="Inferred",
                origin=Origin.AI_INFERRED,
                confidence=Confidence.UNCERTAIN,
            ),
        ],
    )

    receipt = await service.commit(candidates, episode())
    origins = {
        memory_repo.facts[fid].statement: memory_repo.facts[fid].origin
        for fid in receipt.fact_ids
    }

    assert origins["Stated"] is Origin.USER_STATED
    assert origins["Inferred"] is Origin.AI_INFERRED


# ------------------------------------------------------- events and relations


async def test_events_are_committed_with_participants(
    service: MemoryService, memory_repo: FakeMemoryRepository
) -> None:
    candidates = ExtractionCandidates(
        episode_id=EpisodeId(uuid4()),
        events=[
            CandidateEvent(
                description="Argued about the house",
                origin=Origin.USER_STATED,
                participant_names=["Priya"],
                temporal_expression=anchored_expression(),
                salience=0.9,
            )
        ],
    )

    receipt = await service.commit(candidates, episode())
    event = memory_repo.events[receipt.event_ids[0]]

    assert event.occurred_at == datetime(2026, 2, 23, 18, 30, tzinfo=UTC)
    assert len(event.participant_entity_ids) == 1


async def test_relationships_link_two_resolved_entities(
    service: MemoryService, memory_repo: FakeMemoryRepository
) -> None:
    candidates = ExtractionCandidates(
        episode_id=EpisodeId(uuid4()),
        entities=[
            CandidateEntity(name="me", entity_type=EntityType.PERSON),
            CandidateEntity(name="Suresh", entity_type=EntityType.PERSON),
        ],
        relationships=[
            CandidateRelationship(
                from_name="me",
                to_name="Suresh",
                relation_type="friend",
                origin=Origin.USER_STATED,
            )
        ],
    )

    receipt = await service.commit(candidates, episode())
    relationship = memory_repo.relationships[receipt.relationship_ids[0]]

    assert relationship.relation_type == "friend"
    assert relationship.from_entity_id != relationship.to_entity_id


async def test_relationship_with_an_unresolvable_endpoint_is_skipped_not_fatal(
    service: MemoryService, memory_repo: FakeMemoryRepository
) -> None:
    """A malformed relationship must not discard the facts alongside it."""
    candidates = ExtractionCandidates(
        episode_id=EpisodeId(uuid4()),
        facts=[CandidateFact(statement="A useful fact", origin=Origin.USER_STATED)],
        relationships=[
            CandidateRelationship(
                from_name="Suresh",
                to_name="   ",
                relation_type="friend",
                origin=Origin.USER_STATED,
            )
        ],
    )

    receipt = await service.commit(candidates, episode())

    assert receipt.relationship_ids == []
    assert len(receipt.fact_ids) == 1


async def test_relationships_record_provenance(
    service: MemoryService, provenance_repo: FakeProvenanceRepository
) -> None:
    """Relationships were the one memory kind that could have escaped ADR-012 if they
    had no id of their own."""
    source = episode()
    candidates = ExtractionCandidates(
        episode_id=source.id,
        entities=[
            CandidateEntity(name="me", entity_type=EntityType.PERSON),
            CandidateEntity(name="Suresh", entity_type=EntityType.PERSON),
        ],
        relationships=[
            CandidateRelationship(
                from_name="me",
                to_name="Suresh",
                relation_type="friend",
                origin=Origin.USER_STATED,
            )
        ],
    )

    receipt = await service.commit(candidates, source)

    refs = await provenance_repo.for_memory(
        receipt.relationship_ids[0], MemoryKind.RELATIONSHIP
    )
    assert len(refs) == 1


# --------------------------------------------------------------------- ranking


async def test_active_facts_are_ordered_by_salience(
    service: MemoryService, memory_repo: FakeMemoryRepository
) -> None:
    """This is the mechanism that stops trivia burying signal (ADR-017)."""
    candidates = ExtractionCandidates(
        episode_id=EpisodeId(uuid4()),
        facts=[
            CandidateFact(
                statement="Had toast", origin=Origin.USER_STATED, salience=0.15
            ),
            CandidateFact(
                statement="Sister got divorced",
                origin=Origin.USER_STATED,
                salience=0.95,
            ),
        ],
    )
    await service.commit(candidates, episode())

    ranked = await memory_repo.active_facts()

    assert ranked[0].statement == "Sister got divorced"
