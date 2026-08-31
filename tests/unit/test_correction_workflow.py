"""CorrectionWorkflow — the C-26 axis decision and its interrupt.

The test that matters most is `test_correct_and_supersede_touch_different_axes`.
Every other behaviour here can be wrong in a way someone notices; choosing the wrong
axis produces records that still look plausible and only reveal themselves much later
through a `state_at` query nobody thinks to run.

Retrieval is stubbed — its correctness is Unit 4's suite. `MemoryService` is real over
fake repositories, because "which timestamp actually moved" is the question.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from pca.domain.enums import (
    Confidence,
    CorrectionStatus,
    OperationKind,
    Origin,
)
from pca.domain.ids import ConversationId, EpisodeId, MemoryId
from pca.domain.memory import Fact, ProvenanceRef
from pca.domain.orchestration import CorrectionRequest
from pca.domain.retrieval import RetrievalBudget, RetrievalResult
from pca.domain.temporal import BeliefWindow, TemporalValidity
from pca.orchestration.checkpointer import PostgresCheckpointSaver
from pca.orchestration.correction_workflow import CorrectionWorkflow, _Plan
from pca.services.belief_history import BeliefHistoryService
from pca.services.entities import EntityService
from pca.services.extraction import TimeReference
from pca.services.memory import MemoryService
from pca.services.operation_log import MemoryOperationLog
from pca.services.provenance import ProvenanceService
from tests.fakes.checkpoints import FakeCheckpointStore
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

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
BUDGET = RetrievalBudget(
    max_duration=__import__("datetime").timedelta(seconds=5),
    max_items=5,
    max_context_chars=1000,
)


@dataclass
class StubRetrieval:
    facts: list[Fact] = field(default_factory=list)

    def budget_for(self, intent: str | None = None) -> RetrievalBudget:
        return BUDGET

    async def retrieve(self, query) -> RetrievalResult:
        return RetrievalResult(facts=list(self.facts))


class Harness:
    def __init__(self) -> None:
        self.clock = FakeClock(start=NOW, zone="Asia/Kolkata")
        self.memory_repo = FakeMemoryRepository()
        self.store = FakeCheckpointStore()
        self.retrieval = StubRetrieval()
        self.provider = FakeLLMProvider()

        entity_repo = FakeEntityRepository()
        provenance_repo = FakeProvenanceRepository()
        belief_repo = FakeBeliefRepository()
        operation_repo = FakeOperationLogRepository()

        self.memory = MemoryService(
            repository=self.memory_repo,
            entities=EntityService(repository=entity_repo, clock=self.clock),
            provenance=ProvenanceService(
                repository=provenance_repo,
                conversations=FakeConversationRepository(),
                clock=self.clock,
            ),
            clock=self.clock,
            transactions=FakeTransactionManager(
                self.memory_repo,
                entity_repo,
                provenance_repo,
                belief_repo,
                operation_repo,
            ),
            beliefs=BeliefHistoryService(repository=belief_repo, clock=self.clock),
            operations=MemoryOperationLog(repository=operation_repo, clock=self.clock),
        )

    def workflow(self) -> CorrectionWorkflow:
        """A fresh workflow over the same durable store.

        Rebuilt per call so a test can discard it and prove a resume survives more
        than in-process memory.
        """
        return CorrectionWorkflow(
            retrieval=self.retrieval,  # type: ignore[arg-type]
            memory=self.memory,
            memory_repository=self.memory_repo,
            provider=self.provider,
            clock=self.clock,
            checkpointer=PostgresCheckpointSaver(self.store, workflow="correction"),
        )

    async def given_fact(self, statement: str) -> Fact:
        fact = Fact(
            id=MemoryId(uuid4()),
            statement=statement,
            origin=Origin.USER_STATED,
            confidence=Confidence.CERTAIN,
            validity=TemporalValidity(valid_from=datetime(2026, 1, 1, tzinfo=UTC)),
            belief=BeliefWindow(asserted_at=datetime(2026, 1, 1, tzinfo=UTC)),
            provenance=[ProvenanceRef(episode_id=EpisodeId(uuid4()))],
        )
        await self.memory_repo.insert_fact(fact, salience_category=None)
        self.retrieval.facts = [fact]
        return fact

    def plans(self, *plans: _Plan) -> None:
        self.provider._structured = list(plans)  # noqa: SLF001


def _plan(
    operation: str,
    statement: str,
    confidence: float = 0.95,
    when: TimeReference | None = None,
) -> _Plan:
    return _Plan(
        operation=operation,
        target_index=0,
        corrected_statement=statement,
        when=when or TimeReference(raw_phrase="", kind="none"),
        confidence=confidence,
        rationale="test",
    )


def _request(statement: str) -> CorrectionRequest:
    return CorrectionRequest(
        conversation_id=ConversationId(uuid4()),
        statement=statement,
        reason="user said so",
    )


@pytest.fixture
def harness() -> Harness:
    return Harness()


# ------------------------------------------------------------------ C-26 core


async def test_correct_and_supersede_touch_different_axes(harness: Harness) -> None:
    """The distinction the whole workflow exists to protect.

    correct   -> belief ends (retracted_at set), world validity untouched
    supersede -> belief continues, world validity ends (valid_to set)

    Asserting only that "something was written" would pass with the axes swapped,
    and the damage would surface months later as a timeline that quietly disagrees
    with itself.
    """
    mistaken = await harness.given_fact("Priya lives in Bengaluru East")
    harness.plans(_plan("correct", "Priya lives in Bangalore"))
    await harness.workflow().run(_request("No, I said Bangalore"))

    moved = await harness.given_fact("Priya lives in Bangalore")
    harness.plans(_plan("supersede", "Priya lives in Pune"))
    await harness.workflow().run(_request("She moved to Pune"))

    corrected = await harness.memory_repo.get_fact(mistaken.id)
    superseded = await harness.memory_repo.get_fact(moved.id)
    assert corrected is not None and superseded is not None

    # Corrected: never true, so there is no world period to preserve.
    assert corrected.belief.retracted_at is not None
    assert corrected.validity.valid_to is None

    # Superseded: still believed true for its window (FR-04.4).
    assert superseded.belief.retracted_at is None
    assert superseded.validity.valid_to is not None


async def test_a_correction_reports_which_operation_it_applied(
    harness: Harness,
) -> None:
    await harness.given_fact("Priya lives in Bengaluru East")
    harness.plans(_plan("correct", "Priya lives in Bangalore"))

    result = await harness.workflow().run(_request("No, I said Bangalore"))

    assert result.status is CorrectionStatus.APPLIED
    assert result.operation is OperationKind.CORRECT
    assert result.replacement_id is not None


# ---------------------------------------------------------------- interrupts


async def test_weak_signal_asks_instead_of_choosing_an_axis(
    harness: Harness,
) -> None:
    fact = await harness.given_fact("Priya lives in Bangalore")
    harness.plans(_plan("supersede", "Priya lives in Pune", confidence=0.4))

    result = await harness.workflow().run(_request("Priya... Pune?"))

    assert result.status is CorrectionStatus.AWAITING_CONFIRMATION
    assert result.question
    unchanged = await harness.memory_repo.get_fact(fact.id)
    assert unchanged is not None
    assert unchanged.validity.valid_to is None
    assert unchanged.belief.retracted_at is None, "wrote before confirming"


async def test_an_unclear_operation_asks_even_at_high_confidence(
    harness: Harness,
) -> None:
    await harness.given_fact("Priya lives in Bangalore")
    harness.plans(_plan("unclear", "Priya lives in Pune", confidence=0.99))

    result = await harness.workflow().run(_request("that's not right about Priya"))

    assert result.status is CorrectionStatus.AWAITING_CONFIRMATION


async def test_multiple_candidates_confirm_scope_before_writing(
    harness: Harness,
) -> None:
    """`services.md` Workflow 3's "confirm scope" node.

    Picking whichever ranked first is a guess about which record the user meant, and
    a wrong guess silently retracts a fact they never mentioned.
    """
    first = await harness.given_fact("Priya lives in Bangalore")
    second = await harness.given_fact("Priya works at Acme")
    harness.retrieval.facts = [first, second]
    harness.plans(_plan("correct", "Priya lives in Pune", confidence=0.99))

    result = await harness.workflow().run(_request("that's wrong"))

    assert result.status is CorrectionStatus.AWAITING_CONFIRMATION
    assert len(result.options) == 2


async def test_a_dead_planner_asks_rather_than_defaulting_to_an_axis(
    harness: Harness,
) -> None:
    """Unlike conflict detection, this cannot proceed without the model.

    The operation IS the thing being decided, so a provider failure must not resolve
    to whichever axis happens to be the code path's default.
    """
    await harness.given_fact("Priya lives in Bangalore")
    harness.provider._fail_with = RuntimeError("gemini down")  # noqa: SLF001

    result = await harness.workflow().run(_request("that's wrong"))

    assert result.status is CorrectionStatus.AWAITING_CONFIRMATION


# ------------------------------------------------------------------- resume


async def test_resuming_with_changed_supersedes(harness: Harness) -> None:
    fact = await harness.given_fact("Priya lives in Bangalore")
    harness.plans(_plan("unclear", "Priya lives in Pune", confidence=0.2))

    pending = await harness.workflow().run(_request("Priya, Pune"))
    result = await harness.workflow().resume(pending.thread_id, "changed")

    assert result.status is CorrectionStatus.APPLIED
    assert result.operation is OperationKind.SUPERSEDE
    updated = await harness.memory_repo.get_fact(fact.id)
    assert updated is not None and updated.validity.valid_to is not None


async def test_resuming_with_wrong_corrects(harness: Harness) -> None:
    fact = await harness.given_fact("Priya lives in Bangalore")
    harness.plans(_plan("unclear", "Priya lives in Pune", confidence=0.2))

    pending = await harness.workflow().run(_request("Priya, Pune"))
    result = await harness.workflow().resume(pending.thread_id, "wrong")

    assert result.operation is OperationKind.CORRECT
    updated = await harness.memory_repo.get_fact(fact.id)
    assert updated is not None and updated.belief.retracted_at is not None


async def test_an_unrecognised_answer_takes_the_conservative_axis(
    harness: Harness,
) -> None:
    """Correcting only ends a belief.

    Superseding asserts a change in the world that may never have happened, so an
    ambiguous reply must not be read as one.
    """
    fact = await harness.given_fact("Priya lives in Bangalore")
    harness.plans(_plan("unclear", "Priya lives in Pune", confidence=0.2))

    pending = await harness.workflow().run(_request("Priya, Pune"))
    result = await harness.workflow().resume(pending.thread_id, "erm, maybe?")

    assert result.operation is OperationKind.CORRECT
    updated = await harness.memory_repo.get_fact(fact.id)
    assert updated is not None and updated.validity.valid_to is None


async def test_a_pending_correction_survives_a_restart(harness: Harness) -> None:
    """The ADR-006 justification, exercised on a second workflow.

    Both the graph and the saver are discarded between interrupt and resume; only the
    store carries over. Resuming on the same object would prove only that LangGraph
    remembers things within one process.
    """
    fact = await harness.given_fact("Priya lives in Bangalore")
    harness.plans(_plan("unclear", "Priya lives in Pune", confidence=0.2))

    pending = await harness.workflow().run(_request("Priya, Pune"))
    assert pending.status is CorrectionStatus.AWAITING_CONFIRMATION
    planning_calls = len(harness.provider.calls)

    # New process: everything rebuilt except the durable store.
    result = await harness.workflow().resume(pending.thread_id, "changed")

    assert result.status is CorrectionStatus.APPLIED
    updated = await harness.memory_repo.get_fact(fact.id)
    assert updated is not None and updated.validity.valid_to is not None
    # Without this the test passes even if resume re-ran the whole graph: the fake
    # would raise on a second structured call, the planner would fall back to
    # "unclear", and the confirmed answer would still produce a supersede. Asserting
    # the model was not consulted again is what proves state was restored rather
    # than recomputed.
    assert len(harness.provider.calls) == planning_calls, "resume re-planned"


# -------------------------------------------------------------- nothing to do


async def test_nothing_found_writes_nothing(harness: Harness) -> None:
    harness.retrieval.facts = []

    result = await harness.workflow().run(_request("that's wrong"))

    assert result.status is CorrectionStatus.NOTHING_TO_CORRECT
    assert harness.memory_repo.facts == {}
    assert harness.provider.calls == [], "planned a correction with no candidates"


# ------------------------------------------------------------------ ADR-010


async def test_an_unresolvable_date_falls_back_to_now_rather_than_inventing_one(
    harness: Harness,
) -> None:
    """A fabricated date would silently bound the old fact at a time nobody stated.

    "It was true until today" is conservative and honest, and matches what the
    automatic supersession path does with the utterance time.
    """
    fact = await harness.given_fact("Priya lives in Bangalore")
    harness.plans(
        _plan(
            "supersede",
            "Priya lives in Pune",
            when=TimeReference(raw_phrase="a while back", kind="none"),
        )
    )

    await harness.workflow().run(_request("she moved a while back"))

    updated = await harness.memory_repo.get_fact(fact.id)
    assert updated is not None
    assert updated.validity.valid_to == NOW


async def test_a_relative_phrase_is_resolved_by_code_not_the_model(
    harness: Harness,
) -> None:
    """ADR-010: the model produced a descriptor with no date in it."""
    fact = await harness.given_fact("Priya lives in Bangalore")
    harness.plans(
        _plan(
            "supersede",
            "Priya lives in Pune",
            when=TimeReference(
                raw_phrase="three months ago",
                kind="clock_relative",
                direction="past",
                quantity=3,
                unit="month",
            ),
        )
    )

    await harness.workflow().run(_request("she moved three months ago"))

    updated = await harness.memory_repo.get_fact(fact.id)
    assert updated is not None
    assert updated.validity.valid_to is not None
    assert updated.validity.valid_to < NOW, "the phrase was ignored"
