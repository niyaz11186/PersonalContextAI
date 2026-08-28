"""Unit 3 — temporal integrity.

The completion criterion for this unit, stated as executable assertions:

  1. Assert "she lives in Pune". Later assert "she moved to Bangalore in March".
     Both states must be RETAINED. state_at(February) returns Pune, state_at(now)
     returns Bangalore.

  2. Correct a mistaken fact, then verify `believed_at` returns a DIFFERENT answer
     from `state_at` for the SAME date.

Point 2 is the one that cannot be faked by a single-axis implementation. If both
methods read the same column, they can never disagree, and the "two time axes" claim
is decoration. The test below asserts they diverge.

Also covered: commit atomicity, which Unit 2 lacked and which everything else here
depends on — a supersession that wrote the new fact but lost the belief transition
would leave a timeline with no record of why it changed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from pca.domain.conversation import Episode
from pca.domain.enums import (
    BeliefChangeCause,
    Confidence,
    ConflictKind,
    MemoryKind,
    OperationKind,
    Origin,
    SalienceCategory,
)
from pca.domain.errors import MemoryNotFound
from pca.domain.extraction import CandidateFact, ExtractionCandidates
from pca.domain.ids import ConversationId, EpisodeId, MemoryId, MessageId
from pca.services.belief_history import BeliefHistoryService
from pca.services.conflicts import ConflictDetectionService
from pca.services.entities import EntityService
from pca.services.memory import MemoryService
from pca.services.operation_log import MemoryOperationLog
from pca.services.provenance import ProvenanceService
from pca.services.timeline import TimelineService
from tests.fakes.clock import FakeClock
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
from tests.fakes.repositories import FakeConversationRepository

JANUARY = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
FEBRUARY = datetime(2026, 2, 15, 9, 0, tzinfo=UTC)
MARCH = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
JUNE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class Harness:
    """Everything wired against fakes, with the fakes exposed for assertions."""

    def __init__(self, now: datetime = JANUARY) -> None:
        self.clock = FakeClock(start=now, zone="Asia/Kolkata")
        self.memory_repo = FakeMemoryRepository()
        self.entity_repo = FakeEntityRepository()
        self.provenance_repo = FakeProvenanceRepository()
        self.belief_repo = FakeBeliefRepository()
        self.operation_repo = FakeOperationLogRepository()
        self.transactions = FakeTransactionManager(
            self.memory_repo,
            self.entity_repo,
            self.provenance_repo,
            self.belief_repo,
            self.operation_repo,
        )
        self.beliefs = BeliefHistoryService(
            repository=self.belief_repo, clock=self.clock
        )
        self.operations = MemoryOperationLog(
            repository=self.operation_repo, clock=self.clock
        )
        self.service = MemoryService(
            repository=self.memory_repo,
            entities=EntityService(repository=self.entity_repo, clock=self.clock),
            provenance=ProvenanceService(
                repository=self.provenance_repo,
                conversations=FakeConversationRepository(),
                clock=self.clock,
            ),
            clock=self.clock,
            transactions=self.transactions,
            beliefs=self.beliefs,
            operations=self.operations,
        )
        self.timeline = TimelineService(
            memory=self.memory_repo, beliefs=self.belief_repo, clock=self.clock
        )

    def at(self, when: datetime) -> None:
        self.clock.set(when)

    async def commit_fact(
        self, statement: str, subject: str = "Priya", valid_from: datetime | None = None
    ) -> MemoryId:
        ep = self._episode()
        candidates = ExtractionCandidates(
            episode_id=ep.id,
            facts=[
                CandidateFact(
                    statement=statement,
                    subject_names=[subject],
                    origin=Origin.USER_STATED,
                    confidence=Confidence.CERTAIN,
                    salience=0.7,
                    salience_category=SalienceCategory.LOCATION,
                )
            ],
        )
        receipt = await self.service.commit(candidates, ep)
        fact_id = receipt.fact_ids[0]
        if valid_from is not None:
            # The candidate carried no resolved date; set world-time validity directly
            # so the timeline assertions have a defined starting bound.
            import dataclasses

            from pca.domain.temporal import TemporalValidity

            fact = self.memory_repo.facts[fact_id]
            self.memory_repo.facts[fact_id] = dataclasses.replace(
                fact, validity=TemporalValidity(valid_from=valid_from, valid_to=None)
            )
        return fact_id

    def _episode(self) -> Episode:
        return Episode(
            id=EpisodeId(uuid4()),
            content="test episode",
            occurred_at=self.clock.now(),
            zone=self.clock.zone(),
            conversation_id=ConversationId(uuid4()),
            message_id=MessageId(uuid4()),
        )


@pytest.fixture
def harness() -> Harness:
    return Harness()


# --------------------------------------------------------------------- atomicity


async def test_a_failed_commit_leaves_no_trace(harness: Harness) -> None:
    """The Unit 2 failure, now prevented.

    Observed live: facts and entities committed, the relationship insert failed, and
    the episode was left half-written with nothing to signal it. Reproduced here by
    failing the relationship insert.
    """
    harness.memory_repo.fail_on_relationship = True

    from pca.domain.extraction import CandidateRelationship

    ep = harness._episode()
    candidates = ExtractionCandidates(
        episode_id=ep.id,
        facts=[
            CandidateFact(
                statement="Priya lives in Pune",
                subject_names=["Priya"],
                origin=Origin.USER_STATED,
                confidence=Confidence.CERTAIN,
                salience=0.7,
            )
        ],
        relationships=[
            CandidateRelationship(
                from_name="Priya",
                to_name="Pune",
                relation_type="lives_in",
                origin=Origin.USER_STATED,
            )
        ],
    )

    with pytest.raises(RuntimeError):
        await harness.service.commit(candidates, ep)

    assert harness.memory_repo.facts == {}, "the fact must not survive a failed commit"
    assert harness.entity_repo.entities == {}, "entities must roll back too"
    assert harness.provenance_repo.rows == []
    assert harness.belief_repo.transitions == []
    assert harness.operation_repo.entries == []
    assert harness.transactions.rolled_back == 1
    assert harness.transactions.committed == 0


async def test_a_commit_uses_exactly_one_transaction(harness: Harness) -> None:
    """Every write in a commit shares one transaction.

    Asserted on identity rather than on a count, because several independent
    transactions would also produce "some transactions were opened".
    """
    await harness.commit_fact("Priya lives in Pune")

    assert harness.transactions.opened == 1
    assert harness.transactions.committed == 1

    handed_out = (
        harness.memory_repo.write_transactions
        + harness.provenance_repo.write_transactions
        + harness.belief_repo.write_transactions
        + harness.operation_repo.write_transactions
    )
    assert handed_out, "expected writes to receive a transaction"
    assert all(t is not None for t in handed_out), "a write ran outside the transaction"
    assert len({id(t) for t in handed_out}) == 1, "writes spanned several transactions"


async def test_commit_records_belief_and_audit_entries(harness: Harness) -> None:
    fact_id = await harness.commit_fact("Priya lives in Pune")

    trail = await harness.beliefs.trail(fact_id)
    assert [t.cause for t in trail] == [BeliefChangeCause.ASSERTED]

    entries = await harness.operations.recent()
    assert [e.operation for e in entries] == [OperationKind.COMMIT]


# ------------------------------------------------- the completion criterion, part 1


async def test_supersession_retains_both_states_across_time(harness: Harness) -> None:
    """"She lives in Pune", then "she moved to Bangalore in March".

    Both states must remain queryable. state_at(February) is Pune; state_at(June) is
    Bangalore. This is FR-04.4 and FR-04.5 together.
    """
    harness.at(JANUARY)
    original_id = await harness.commit_fact("Priya lives in Pune", valid_from=JANUARY)

    harness.at(JUNE)
    outcome = await harness.service.supersede(
        original_id,
        new_statement="Priya lives in Bangalore",
        effective_from=MARCH,
        reason="she moved",
    )

    in_february = [f.statement for f in await harness.timeline.state_at(FEBRUARY)]
    in_june = [f.statement for f in await harness.timeline.state_at(JUNE)]

    assert in_february == ["Priya lives in Pune"], (
        "the earlier state must survive supersession — otherwise 'where did she live "
        "before?' has no answer"
    )
    assert in_june == ["Priya lives in Bangalore"]

    # Both rows still exist. The old one is bounded, not deleted.
    original = harness.memory_repo.facts[original_id]
    replacement = harness.memory_repo.facts[outcome.replacement_id]
    assert original.validity.valid_to == MARCH
    assert replacement.validity.valid_from == MARCH


async def test_supersession_preserves_belief_and_ends_only_world_validity(
    harness: Harness,
) -> None:
    """The axis distinction, asserted directly.

    A supersession must NOT retract the old belief. We still think the fact was true
    for its window; retracting it would erase the history the previous test relies on.
    """
    harness.at(JANUARY)
    original_id = await harness.commit_fact("Priya lives in Pune", valid_from=JANUARY)

    harness.at(JUNE)
    await harness.service.supersede(
        original_id, "Priya lives in Bangalore", effective_from=MARCH
    )

    original = harness.memory_repo.facts[original_id]
    assert original.belief.retracted_at is None, (
        "supersession must leave belief intact; only world validity ends"
    )
    assert original.validity.valid_to == MARCH
    assert original.superseded_by is not None


async def test_supersession_is_audited(harness: Harness) -> None:
    harness.at(JANUARY)
    original_id = await harness.commit_fact("Priya lives in Pune", valid_from=JANUARY)
    harness.at(JUNE)
    await harness.service.supersede(
        original_id, "Priya lives in Bangalore", effective_from=MARCH
    )

    entries = await harness.operations.history_for(original_id)
    assert [e.operation for e in entries] == [OperationKind.SUPERSEDE]
    assert entries[0].detail["previous"] == "Priya lives in Pune"
    assert entries[0].detail["current"] == "Priya lives in Bangalore"


# ------------------------------------------------- the completion criterion, part 2


async def test_correction_makes_the_two_axes_diverge(harness: Harness) -> None:
    """The load-bearing test for this unit.

    In January the system is told Priya works at Google. In June it learns that was
    wrong — Microsoft, and she never worked at Google.

        state_at(February)     -> Microsoft   (the Google fact was never true)
        believed_at(February)  -> Google      (that is what the system thought then)

    If these two ever return the same thing, the system is storing one axis and
    labelling it two.
    """
    harness.at(JANUARY)
    fact_id = await harness.commit_fact(
        "Priya works at Google", subject="Priya", valid_from=JANUARY
    )

    harness.at(JUNE)
    await harness.service.correct(
        fact_id,
        corrected_statement="Priya works at Microsoft",
        reason="user said Google was a mistake",
    )

    comparison = await harness.timeline.compare(FEBRUARY)

    assert comparison.was_true == ["Priya works at Microsoft"], (
        "world axis reflects the corrected knowledge: the Google fact was never true"
    )
    assert "Priya works at Google" in comparison.was_believed, (
        "belief axis must still report the mistaken belief held in February"
    )
    assert comparison.differs, (
        "the two axes MUST diverge after a correction; if they agree, only one axis "
        "is really being stored"
    )


async def test_correction_ends_belief_and_leaves_world_validity_alone(
    harness: Harness,
) -> None:
    """The mirror image of the supersession axis test."""
    harness.at(JANUARY)
    fact_id = await harness.commit_fact("Priya works at Google", valid_from=JANUARY)
    original_validity = harness.memory_repo.facts[fact_id].validity

    harness.at(JUNE)
    outcome = await harness.service.correct(
        fact_id, "Priya works at Microsoft", reason="mistake"
    )

    original = harness.memory_repo.facts[fact_id]
    assert original.belief.retracted_at == JUNE, "belief must end on correction"
    assert original.validity == original_validity, (
        "a correction says the RECORD was wrong, not that the world changed — world "
        "validity must be untouched"
    )

    replacement = harness.memory_repo.facts[outcome.replacement_id]
    assert replacement.validity == original_validity, (
        "the replacement inherits the same world-time window"
    )
    assert harness.memory_repo.corrected_from[outcome.replacement_id] == fact_id


async def test_correction_trail_records_both_beliefs(harness: Harness) -> None:
    harness.at(JANUARY)
    fact_id = await harness.commit_fact("Priya works at Google", valid_from=JANUARY)
    harness.at(JUNE)
    await harness.service.correct(fact_id, "Priya works at Microsoft", reason="mistake")

    trail = await harness.beliefs.trail(fact_id)
    causes = [t.cause for t in trail]
    assert BeliefChangeCause.CORRECTED in causes

    # The snapshotted statement is the whole reason belief_history stores text rather
    # than a reference: the live fact row no longer says "Google".
    corrected = next(t for t in trail if t.cause is BeliefChangeCause.CORRECTED)
    assert corrected.statement == "Priya works at Google"
    assert corrected.reason == "mistake"


async def test_believed_at_before_assertion_returns_nothing(harness: Harness) -> None:
    """Belief windows are bounded at the start as well as the end."""
    harness.at(JUNE)
    await harness.commit_fact("Priya lives in Pune")

    earlier = await harness.timeline.believed_at(JANUARY)
    assert earlier == [], "the system cannot have believed something before it was told"


# ---------------------------------------------------------------------- retraction


async def test_retraction_ends_belief_without_a_replacement(harness: Harness) -> None:
    harness.at(JANUARY)
    fact_id = await harness.commit_fact("Priya lives in Pune", valid_from=JANUARY)

    harness.at(JUNE)
    await harness.service.retract(fact_id, reason="source deleted")

    fact = harness.memory_repo.facts[fact_id]
    assert fact.belief.retracted_at == JUNE
    assert await harness.timeline.state_at(JUNE) == []
    # Still believed in February, which is what makes the retraction auditable.
    assert await harness.timeline.believed_at(FEBRUARY)


async def test_retraction_of_a_missing_fact_raises(harness: Harness) -> None:
    """Silence would let a caller think a deliberate change had been applied."""
    with pytest.raises(MemoryNotFound):
        await harness.service.retract(MemoryId(uuid4()), reason="nope")


async def test_correcting_a_missing_fact_raises(harness: Harness) -> None:
    with pytest.raises(MemoryNotFound):
        await harness.service.correct(MemoryId(uuid4()), "anything", reason="nope")


# ------------------------------------------------------------------------- diff


async def test_diff_separates_ceased_from_corrected(harness: Harness) -> None:
    """FR-04.6, with the distinction that makes it useful.

    "Stopped being true" and "we were wrong" are different events. Reporting a
    correction as a change in the world would tell the user their life changed when in
    fact the record was simply fixed.
    """
    harness.at(JANUARY)
    moved_id = await harness.commit_fact("Priya lives in Pune", valid_from=JANUARY)
    wrong_id = await harness.commit_fact(
        "Priya works at Google", subject="Priya", valid_from=JANUARY
    )

    harness.at(JUNE)
    await harness.service.supersede(
        moved_id, "Priya lives in Bangalore", effective_from=MARCH
    )
    await harness.service.correct(
        wrong_id, "Priya works at Microsoft", reason="mistake"
    )

    diff = await harness.timeline.diff(FEBRUARY, JUNE)

    assert "Priya lives in Bangalore" in diff.became_true
    assert "Priya lives in Pune" in diff.ceased_to_be_true
    assert "Priya works at Google" in diff.corrected
    assert "Priya works at Google" not in diff.ceased_to_be_true, (
        "a correction is not a change in the world"
    )


async def test_diff_rejects_a_reversed_window(harness: Harness) -> None:
    with pytest.raises(ValueError):
        await harness.timeline.diff(JUNE, JANUARY)


# ------------------------------------------------------------- conflict detection


def conflict_service(kinds: list[str]) -> tuple[ConflictDetectionService, FakeMemoryRepository]:
    from pca.services.conflicts import _Classification

    provider = FakeLLMProvider(
        structured_results=[
            _Classification(kind=kind, explanation=f"classified as {kind}")
            for kind in kinds
        ]
    )
    repo = FakeMemoryRepository()
    return ConflictDetectionService(memory=repo, llm=provider), repo


async def test_temporal_change_maps_to_supersession_not_contradiction() -> None:
    """The expensive mistake, guarded.

    Treating every change as a contradiction would ask the user to arbitrate ordinary
    life events until they stopped reading the prompts.
    """
    service, _ = conflict_service(["temporal_change"])
    conflicts = [
        __import__("pca.domain.memory", fromlist=["Conflict"]).Conflict(
            kind=ConflictKind.TEMPORAL_CHANGE,
            incoming_statement="Priya lives in Bangalore",
            existing_memory_id=MemoryId(uuid4()),
            explanation="she moved",
        )
    ]
    assert service.supersessions(conflicts) == conflicts
    assert service.contradictions(conflicts) == []


async def test_contradiction_is_never_auto_resolved() -> None:
    """FR-05.6: surface, never pick a winner.

    Asserted structurally — there is no `resolve` method to call.
    """
    service, _ = conflict_service(["contradiction"])
    assert not hasattr(service, "resolve")


async def test_unrecognised_classification_defaults_to_contradiction() -> None:
    """Fail toward asking rather than toward silence.

    An extra question is recoverable. Treating an unparseable response as "these
    agree" would let a real contradiction through unnoticed.
    """
    from pca.services.conflicts import _parse_kind

    assert _parse_kind("something the model made up") is ConflictKind.CONTRADICTION
    assert _parse_kind("temporal_change") is ConflictKind.TEMPORAL_CHANGE
    assert _parse_kind("  AGREEMENT  ") is ConflictKind.AGREEMENT


async def test_detection_failure_does_not_fail_the_commit() -> None:
    """An undetected conflict is a missed question; a failed commit loses the memory."""

    class Exploding:
        async def structured(self, prompt, schema, *, model=None):
            raise RuntimeError("provider down")

    repo = FakeMemoryRepository()
    service = ConflictDetectionService(memory=repo, llm=Exploding())  # type: ignore[arg-type]

    candidates = ExtractionCandidates(
        episode_id=EpisodeId(uuid4()),
        facts=[
            CandidateFact(
                statement="Priya lives in Pune",
                subject_names=["Priya"],
                origin=Origin.USER_STATED,
                confidence=Confidence.CERTAIN,
            )
        ],
    )
    assert await service.detect(candidates) == []


async def test_agreement_is_not_surfaced_as_a_conflict(harness: Harness) -> None:
    """Corroboration is provenance's job, not the conflict list's."""
    harness.at(JANUARY)
    await harness.commit_fact("Priya lives in Pune")

    from pca.services.conflicts import _Classification

    provider = FakeLLMProvider(
        structured_results=[
            _Classification(kind="agreement", explanation="same thing")
        ]
    )
    service = ConflictDetectionService(memory=harness.memory_repo, llm=provider)

    candidates = ExtractionCandidates(
        episode_id=EpisodeId(uuid4()),
        facts=[
            CandidateFact(
                statement="Priya lives in Pune",
                subject_names=["Priya"],
                origin=Origin.USER_STATED,
                confidence=Confidence.CERTAIN,
            )
        ],
    )
    assert await service.detect(candidates) == []


# ------------------------------------------------------------------ append-only


async def test_operation_log_exposes_no_mutation_path() -> None:
    """An audit trail that can be rewritten is not an audit trail."""
    from pca.adapters.postgres.history_repositories import (
        PostgresOperationLogRepository,
    )

    forbidden = {"update", "delete", "remove", "edit", "clear"}
    present = {
        name
        for name in dir(PostgresOperationLogRepository)
        if not name.startswith("_")
    }
    assert not (forbidden & present), (
        f"operation log must be append-only; found {forbidden & present}"
    )


async def test_belief_history_exposes_no_delete_path() -> None:
    from pca.adapters.postgres.history_repositories import PostgresBeliefRepository

    forbidden = {"delete", "remove", "clear", "purge"}
    present = {
        name for name in dir(PostgresBeliefRepository) if not name.startswith("_")
    }
    assert not (forbidden & present)


async def test_repeated_correction_does_not_rewrite_an_earlier_belief_window(
    harness: Harness,
) -> None:
    """`close_open_transition` is guarded on `retracted_at IS NULL`.

    Without the guard, a second correction would move the end of the FIRST belief
    window, and the trail would stop reflecting what actually happened.
    """
    harness.at(JANUARY)
    fact_id = await harness.commit_fact("Priya works at Google", valid_from=JANUARY)

    harness.at(JUNE)
    first = await harness.service.correct(fact_id, "Priya works at Microsoft", reason="a")
    closed_at = next(
        t.belief.retracted_at
        for t in await harness.beliefs.trail(fact_id)
        if t.cause is BeliefChangeCause.CORRECTED
    )

    harness.at(JUNE + timedelta(days=30))
    await harness.service.correct(first.replacement_id, "Priya works at Amazon", reason="b")

    still_closed_at = next(
        t.belief.retracted_at
        for t in await harness.beliefs.trail(fact_id)
        if t.cause is BeliefChangeCause.CORRECTED
    )
    assert still_closed_at == closed_at
