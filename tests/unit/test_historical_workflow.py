"""Unit 5 Step 10 — HistoricalAnalysisWorkflow.

The plan's requirement: *"what was true"* must reach `TimelineService.state_at` and
*"what did I think"* must reach `BeliefHistoryService.believed_at`, and getting this
wrong produces a confidently wrong answer, so it is asserted directly.

"Asserted directly" is doing real work here. A test that only checked the workflow
returns *something* for both question types would pass against an implementation that
reads world time for both — the wrong answer is still a plausible-looking list of
facts. So these tests assert on **which service was actually consulted**, and that the
two paths return genuinely different content for the same date.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from pca.domain.enums import (
    BeliefChangeCause,
    Confidence,
    MemoryKind,
    Origin,
)
from pca.domain.history import BeliefTransition, TimelineDiff
from pca.domain.ids import ConversationId, EpisodeId, MemoryId, MessageId
from pca.domain.memory import Fact, ProvenanceRef
from pca.domain.orchestration import HistoricalQuery
from pca.domain.temporal import BeliefWindow, TemporalValidity
from pca.orchestration.historical_workflow import (
    HistoricalAnalysisWorkflow,
    _Interpretation,
)
from pca.services.extraction import TimeReference
from tests.fakes.clock import FakeClock
from tests.fakes.llm import FakeLLMProvider

JANUARY = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
MARCH = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
JUNE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def ref() -> ProvenanceRef:
    return ProvenanceRef(
        episode_id=EpisodeId(uuid4()),
        conversation_id=ConversationId(uuid4()),
        message_id=MessageId(uuid4()),
    )


def fact(statement: str) -> Fact:
    return Fact(
        id=MemoryId(uuid4()),
        statement=statement,
        origin=Origin.USER_STATED,
        confidence=Confidence.CERTAIN,
        validity=TemporalValidity(valid_from=JANUARY),
        belief=BeliefWindow(asserted_at=JANUARY),
        provenance=[ref()],
        salience=0.6,
    )


def transition(statement: str) -> BeliefTransition:
    return BeliefTransition(
        id=MemoryId(uuid4()),
        memory_id=MemoryId(uuid4()),
        memory_kind=MemoryKind.FACT,
        cause=BeliefChangeCause.ASSERTED,
        belief=BeliefWindow(asserted_at=JANUARY),
        statement=statement,
        validity=TemporalValidity(valid_from=JANUARY),
        recorded_at=JANUARY,
    )


class SpyTimeline:
    """Records which axis was read, and returns world-time content only."""

    def __init__(self) -> None:
        self.state_at_calls: list[datetime] = []
        self.diff_calls: list[tuple[datetime, datetime]] = []
        self.compare_calls: list[datetime] = []

    async def state_at(self, when: datetime, limit: int = 50):  # type: ignore[no-untyped-def]
        self.state_at_calls.append(when)
        return [fact("Priya works at Microsoft")]

    async def diff(self, start: datetime, end: datetime) -> TimelineDiff:
        self.diff_calls.append((start, end))
        return TimelineDiff(
            start=start,
            end=end,
            became_true=["Priya works at Microsoft"],
            ceased_to_be_true=[],
            corrected=["Priya works at Google"],
        )

    async def compare(self, when: datetime, limit: int = 50):  # type: ignore[no-untyped-def]
        self.compare_calls.append(when)
        return object()


class SpyBeliefs:
    """Records belief-axis reads, and returns the superseded content."""

    def __init__(self) -> None:
        self.believed_at_calls: list[datetime] = []

    async def believed_at(self, when: datetime, limit: int = 50):  # type: ignore[no-untyped-def]
        self.believed_at_calls.append(when)
        return [transition("Priya works at Google")]


def build(
    interpretation: _Interpretation | None = None,
    *,
    provider: FakeLLMProvider | None = None,
) -> tuple[HistoricalAnalysisWorkflow, SpyTimeline, SpyBeliefs]:
    timeline = SpyTimeline()
    beliefs = SpyBeliefs()
    llm = provider or FakeLLMProvider(
        structured_results=[interpretation] if interpretation else []
    )
    workflow = HistoricalAnalysisWorkflow(
        timeline=timeline,  # type: ignore[arg-type]
        beliefs=beliefs,  # type: ignore[arg-type]
        provider=llm,  # type: ignore[arg-type]
        clock=FakeClock(start=JUNE, zone="Asia/Kolkata"),
    )
    return workflow, timeline, beliefs


def interpretation(
    axis: str = "world", is_range: bool = False, phrase: str = ""
) -> _Interpretation:
    kind = "clock_relative" if phrase else "none"
    return _Interpretation(
        axis=axis,
        is_range=is_range,
        when=TimeReference(raw_phrase=phrase, kind=kind),  # type: ignore[arg-type]
        until=TimeReference(raw_phrase="", kind="none"),
        rationale="test",
    )


def query(text: str, **kwargs) -> HistoricalQuery:  # type: ignore[no-untyped-def]
    return HistoricalQuery(
        conversation_id=ConversationId(uuid4()), text=text, **kwargs
    )


# ------------------------------------------------------------- the axis split


async def test_what_was_true_reads_world_time() -> None:
    workflow, timeline, beliefs = build(interpretation(axis="world"))

    result = await workflow.run(query("Where did Priya work in March?"))

    assert timeline.state_at_calls, "world question must reach TimelineService.state_at"
    assert not beliefs.believed_at_calls, (
        "world question must NOT read the belief axis — reporting a retracted belief "
        "as fact asserts something the system knows to be false"
    )
    assert result.axis == "world"
    assert [f.statement for f in result.world_facts] == ["Priya works at Microsoft"]


async def test_what_did_i_think_reads_belief_time() -> None:
    workflow, timeline, beliefs = build(interpretation(axis="belief"))

    result = await workflow.run(query("What did I think about Priya back then?"))

    assert beliefs.believed_at_calls, (
        "belief question must reach BeliefHistoryService.believed_at"
    )
    assert not timeline.state_at_calls, (
        "belief question must NOT read world time — that would claim the system was "
        "right all along and erase the audit trail"
    )
    assert result.axis == "belief"
    assert [b.statement for b in result.beliefs] == ["Priya works at Google"]


async def test_the_two_axes_return_different_answers_for_the_same_date() -> None:
    """The divergence is the point, not an edge case.

    If these ever agree, the system is reading one axis and labelling it two — which
    is precisely what Unit 3's completion criterion was written to catch, restated
    here at the workflow level.
    """
    world_workflow, _, _ = build(interpretation(axis="world"))
    belief_workflow, _, _ = build(interpretation(axis="belief"))

    world = await world_workflow.run(query("Where did she work in March?"))
    belief = await belief_workflow.run(query("What did I think in March?"))

    world_statements = {f.statement for f in world.world_facts}
    belief_statements = {b.statement for b in belief.beliefs}

    assert world_statements != belief_statements, (
        "the axes must be able to disagree for the same instant"
    )
    assert "Microsoft" in next(iter(world_statements))
    assert "Google" in next(iter(belief_statements))


def test_route_is_a_pure_decision_that_can_be_asserted_alone() -> None:
    """Kept as a pure function so the decision is testable without a graph run.

    The routing is the part that produces a confidently wrong answer when it breaks,
    so it should not require mocking three services to check.
    """
    assert (
        HistoricalAnalysisWorkflow._route({"interpretation": interpretation("world")})
        == "world"
    )
    assert (
        HistoricalAnalysisWorkflow._route({"interpretation": interpretation("belief")})
        == "belief"
    )
    assert (
        HistoricalAnalysisWorkflow._route(
            {"interpretation": interpretation("world", is_range=True)}
        )
        == "range"
    )


def test_a_range_question_routes_to_diff_regardless_of_axis() -> None:
    """"What changed" is inherently a world-time question, and TimelineDiff already
    separates 'stopped being true' from 'we were wrong', so the belief dimension is
    not lost by routing here."""
    assert (
        HistoricalAnalysisWorkflow._route(
            {"interpretation": interpretation("belief", is_range=True)}
        )
        == "range"
    )


# ------------------------------------------------------------------- ranges


async def test_a_range_question_computes_a_diff() -> None:
    workflow, timeline, _ = build(interpretation(is_range=True))

    result = await workflow.run(
        query("What changed?", start=MARCH, end=JUNE)
    )

    assert timeline.diff_calls == [(MARCH, JUNE)]
    assert result.is_range is True
    assert result.diff is not None
    assert "Priya works at Google" in result.diff.corrected


async def test_a_reversed_range_is_normalised_rather_than_raising() -> None:
    """The user saying "between June and March" means the same period. Raising would
    turn a harmless phrasing into an error."""
    workflow, timeline, _ = build(interpretation(is_range=True))

    await workflow.run(query("What changed?", start=JUNE, end=MARCH))

    start, end = timeline.diff_calls[0]
    assert start < end


# ------------------------------------------------------ time resolution (ADR-010)


async def test_an_explicit_range_on_the_request_wins_over_parsed_text() -> None:
    """A caller that supplied dates knows more than a classifier can infer."""
    workflow, timeline, _ = build(interpretation(axis="world", phrase="three weeks ago"))

    await workflow.run(query("Where was she?", start=MARCH, end=JUNE))

    assert timeline.state_at_calls == [MARCH]


async def test_an_unresolvable_phrase_is_disclosed_rather_than_guessed() -> None:
    """ADR-010 leaves dates null rather than fabricating them. On a question
    explicitly about a period, a fabricated date answers about the wrong period with
    no signal to the user — so the result carries the phrase that could not be
    resolved."""
    workflow, _, _ = build(
        interpretation(axis="world", phrase="before the wedding")
    )
    # 'before the wedding' is event_relative, not clock_relative, so it cannot be
    # resolved to a date by arithmetic.
    unresolvable = _Interpretation(
        axis="world",
        is_range=False,
        when=TimeReference(
            raw_phrase="before the wedding",
            kind="event_relative",
            reference_event="the wedding",
            ordering="before",
        ),
        until=TimeReference(raw_phrase="", kind="none"),
        rationale="event relative",
    )
    workflow, timeline, _ = build(unresolvable)

    result = await workflow.run(query("Where was she before the wedding?"))

    # Falls back to now rather than inventing a date, and still answers.
    assert timeline.state_at_calls == [JUNE]
    assert result.world_facts


async def test_a_clock_relative_phrase_is_resolved_by_code_not_the_model() -> None:
    """The model returns structure; TimeResolver computes the date.

    'three weeks ago' from a June 1 anchor must land in May, computed deterministically
    — the whole reason ADR-010 forbids letting the model return dates.
    """
    three_weeks_ago = _Interpretation(
        axis="world",
        is_range=False,
        when=TimeReference(
            raw_phrase="three weeks ago",
            kind="clock_relative",
            direction="past",
            quantity=3,
            unit="week",
        ),
        until=TimeReference(raw_phrase="", kind="none"),
        rationale="offset",
    )
    workflow, timeline, _ = build(three_weeks_ago)

    await workflow.run(query("Where was she three weeks ago?"))

    resolved = timeline.state_at_calls[0]
    assert resolved < JUNE
    assert (JUNE - resolved) >= timedelta(days=20)


# --------------------------------------------------------------- robustness


async def test_an_explicit_belief_request_skips_the_model_entirely() -> None:
    """The caller set `about_belief`; spending a classification call to second-guess it
    would be slower and less accurate."""
    provider = FakeLLMProvider(structured_results=[])
    workflow, timeline, beliefs = build(provider=provider)

    result = await workflow.run(
        query("anything", about_belief=True, start=MARCH, end=None)
    )

    assert not any(call[0] == "structured" for call in provider.calls), (
        "no model call should be made when the axis is already known"
    )
    assert beliefs.believed_at_calls, "should still read the belief axis"
    assert result.axis == "belief"


async def test_interpretation_failure_defaults_to_the_world_axis() -> None:
    """Being wrong toward world time is the less damaging default: reporting what was
    true when asked what we thought is unhelpful, whereas reporting a retracted belief
    as fact asserts something known false."""
    provider = FakeLLMProvider(fail_with=RuntimeError("provider down"))
    workflow, timeline, beliefs = build(provider=provider)

    result = await workflow.run(query("What did I think in March?"))

    assert result.axis == "world"
    assert timeline.state_at_calls
    assert not beliefs.believed_at_calls


async def test_the_result_reports_which_axis_it_read() -> None:
    """The axis is part of the answer, not metadata. A caller cannot otherwise tell
    whether "you thought X" was built from belief data or world data."""
    workflow, _, _ = build(interpretation(axis="belief"))

    result = await workflow.run(query("What did I think?"))

    assert result.axis == "belief"
