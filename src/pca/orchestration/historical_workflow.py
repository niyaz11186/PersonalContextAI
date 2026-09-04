"""HistoricalAnalysisWorkflow — answering questions about the past.

Layer L2. Imports `langgraph`.

This workflow exists for one routing decision, and it is the same distinction Unit 3
built the two time axes to support:

    "Where did Priya live in March?"       -> WORLD time   TimelineService.state_at
    "What did I think about her in March?" -> BELIEF time  believed_at

Those return different answers whenever a correction landed in between, and that
divergence is the point rather than an edge case. Suppose in March the user said
Priya works at Google, and in June corrected it to Microsoft — she never worked at
Google at all:

    state_at(March)     -> Microsoft   the Google fact was never true, so it is
                                       absent from the world timeline entirely
    believed_at(March)  -> Google      because that is genuinely what the system
                                       thought at the time

Answering the world question with belief data asserts something the system knows to
be false. Answering the belief question with world data claims the system was right
all along and erases the audit trail. Both are confidently wrong rather than merely
unhelpful, which is why `_route` is asserted directly in the tests instead of being
left as an implementation detail.

Two other things worth stating:

**ADR-010 division of labour holds here too.** The model parses the time phrase into
a structure; `TimeResolver` computes the dates. Letting the model return dates would
put unfalsifiable arithmetic on the one path whose entire purpose is temporal
accuracy.

**No checkpointer.** This workflow never interrupts — it reads, it answers, it ends.
Attaching a checkpointer would add durable writes to a pure read path for no benefit.
`ClarificationWorkflow` is where ADR-006's LangGraph dependency actually earns itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from pca.domain.enums import Granularity
from pca.domain.history import BeliefTransition, TimelineDiff
from pca.domain.memory import Fact
from pca.domain.orchestration import HistoricalQuery
from pca.domain.temporal import (
    RelativeDescriptor,
    TemporalDirection,
    TemporalModifier,
    TemporalUnit,
)
from pca.observability.logging import get_logger
from pca.ports.clock import ClockPort
from pca.ports.llm import LLMProviderPort, Prompt, PromptMessage
from pca.services.belief_history import BeliefHistoryService
from pca.services.extraction import TimeReference
from pca.services.time_resolver import TimeResolver
from pca.services.timeline import TimelineService

_log = get_logger(__name__)

_DEFAULT_LIMIT = 50

_SYSTEM = """\
The user is asking about the past. Work out two things.

1. WHICH AXIS they are asking about. These are different questions:

   world   What was actually the case at that time.
           "Where did she live in March?"  "Was I still at that job in 2025?"

   belief  What I (the assistant) thought or had been told at that time. Asking
           about the record itself, not the world.
           "What did I think back then?"  "When did I tell you about the move?"
           "Did you know about it in March?"

   Choose world unless the question is genuinely about the state of my knowledge.
   Most questions are about the world.

2. THE TIME PHRASE, as structure only. Do not compute or return any date. If the
   question names no time at all, set kind to "none".

Also decide whether they are asking about a single moment or about what CHANGED over
a period. "What changed between March and June" is a range; "where did she live in
March" is a moment.
"""


def _keep_last(_existing: object, new: object) -> object:
    return new


class _Interpretation(BaseModel):
    axis: Literal["world", "belief"] = Field(
        description="world = what was true; belief = what I thought"
    )
    is_range: bool = Field(
        description="true if asking what changed over a period rather than the state "
        "at one moment"
    )
    when: TimeReference = Field(description="the time phrase, as structure. No dates.")
    until: TimeReference = Field(
        description="for a range, the end phrase. kind='none' when not a range."
    )
    rationale: str = Field(description="one short clause")


class HistoricalState(TypedDict, total=False):
    query: HistoricalQuery
    interpretation: Annotated[_Interpretation, _keep_last]
    resolved_start: Annotated[datetime, _keep_last]
    resolved_end: Annotated[datetime, _keep_last]
    world_facts: Annotated[list[Fact], _keep_last]
    beliefs: Annotated[list[BeliefTransition], _keep_last]
    diff: Annotated[TimelineDiff, _keep_last]
    answer_axis: Annotated[str, _keep_last]


class HistoricalAnalysisResult(BaseModel):
    """What the workflow found. Deliberately reports which axis it read.

    The axis is part of the answer, not metadata. A reply that says "you thought X"
    when it actually read world time is a different claim than intended, and the
    caller cannot tell without being told which axis was consulted.
    """

    model_config = {"arbitrary_types_allowed": True}

    axis: str
    is_range: bool = False
    when: datetime | None = None
    until: datetime | None = None
    world_facts: list[Fact] = Field(default_factory=list)
    beliefs: list[BeliefTransition] = Field(default_factory=list)
    diff: TimelineDiff | None = None
    unresolved_phrase: str | None = None
    """Set when a time phrase could not be resolved to a date. The caller must
    disclose this rather than silently answering about the wrong period (ADR-010
    leaves dates null rather than guessing)."""


class HistoricalAnalysisWorkflow:
    def __init__(
        self,
        timeline: TimelineService,
        beliefs: BeliefHistoryService,
        provider: LLMProviderPort,
        clock: ClockPort,
        resolver: TimeResolver | None = None,
        model: str | None = None,
    ) -> None:
        self._timeline = timeline
        self._beliefs = beliefs
        self._provider = provider
        self._clock = clock
        self._resolver = resolver or TimeResolver()
        self._model = model
        self._graph = self._build()

    # ------------------------------------------------------------------- graph

    def _build(self):  # type: ignore[no-untyped-def]
        builder = StateGraph(HistoricalState)
        builder.add_node("interpret", self._interpret)
        builder.add_node("resolve_time", self._resolve_time)
        builder.add_node("read_world", self._read_world)
        builder.add_node("read_belief", self._read_belief)
        builder.add_node("compute_diff", self._compute_diff)

        builder.add_edge(START, "interpret")
        builder.add_edge("interpret", "resolve_time")
        # The routing split. Conditional rather than reading both and choosing later:
        # reading both would double the query cost and, worse, invite a caller to
        # present belief data under a world-time heading because both were available.
        builder.add_conditional_edges(
            "resolve_time",
            self._route,
            {
                "world": "read_world",
                "belief": "read_belief",
                "range": "compute_diff",
            },
        )
        builder.add_edge("read_world", END)
        builder.add_edge("read_belief", END)
        builder.add_edge("compute_diff", END)
        return builder.compile()

    # ------------------------------------------------------------------- nodes

    async def _interpret(self, state: HistoricalState) -> HistoricalState:
        query = state["query"]

        # An explicit `about_belief` on the request skips the model entirely. The
        # caller that set it knows more than a classifier can infer, and spending a
        # model call to second-guess it would be slower and less accurate.
        if query.about_belief:
            return {
                "interpretation": _Interpretation(
                    axis="belief",
                    is_range=query.start is not None and query.end is not None,
                    when=TimeReference(raw_phrase="", kind="none"),
                    until=TimeReference(raw_phrase="", kind="none"),
                    rationale="caller specified the belief axis explicitly",
                )
            }

        prompt = Prompt(
            system=_SYSTEM,
            messages=[PromptMessage(role="user", content=query.text)],
            temperature=0.0,
        )
        try:
            interpretation = await self._provider.structured(
                prompt, _Interpretation, model=self._model
            )
        except Exception as exc:  # noqa: BLE001
            # Default to the world axis. It is the common case, and being wrong here
            # produces a less damaging answer than defaulting to belief: reporting
            # what was true when asked what we thought is merely unhelpful, whereas
            # reporting a retracted belief as fact asserts something known false.
            _log.warning(
                "historical_interpretation_failed",
                error=str(exc)[:200],
                defaulted_to="world axis, single moment",
            )
            interpretation = _Interpretation(
                axis="world",
                is_range=False,
                when=TimeReference(raw_phrase="", kind="none"),
                until=TimeReference(raw_phrase="", kind="none"),
                rationale="interpretation unavailable; defaulted to world axis",
            )

        _log.info(
            "historical_interpreted",
            axis=interpretation.axis,
            is_range=interpretation.is_range,
            rationale=interpretation.rationale,
        )
        return {"interpretation": interpretation}

    async def _resolve_time(self, state: HistoricalState) -> HistoricalState:
        """Compute dates from the parsed structure. ADR-010: the model never does this."""
        query = state["query"]
        interpretation = state["interpretation"]
        now = self._clock.now()

        # An explicit range on the request wins over anything parsed from the text.
        if query.start is not None:
            return {
                "resolved_start": query.start,
                "resolved_end": query.end or now,
            }

        start = self._resolve_one(interpretation.when, now)
        end = self._resolve_one(interpretation.until, now)

        if start is None:
            # No usable date. `now` is the honest fallback: it is the one instant the
            # system can state without inventing anything, and the result carries
            # `unresolved_phrase` so the caller discloses rather than implying the
            # answer covers the period the user named.
            start = now

        return {
            "resolved_start": start,
            "resolved_end": end or now,
        }

    async def _read_world(self, state: HistoricalState) -> HistoricalState:
        """What was true (FR-04.5)."""
        when = state["resolved_start"]
        facts = await self._timeline.state_at(when, limit=_DEFAULT_LIMIT)
        _log.info("historical_world_read", when=when.isoformat(), facts=len(facts))
        return {"world_facts": list(facts), "answer_axis": "world"}

    async def _read_belief(self, state: HistoricalState) -> HistoricalState:
        """What the system believed (FR-04.8).

        Reads `BeliefHistoryService` rather than `TimelineService.state_at`. These are
        separate services precisely because the method names are otherwise easy to
        confuse, and calling the wrong one here is the defect this workflow exists to
        avoid.
        """
        when = state["resolved_start"]
        held = await self._beliefs.believed_at(when, limit=_DEFAULT_LIMIT)
        _log.info("historical_belief_read", when=when.isoformat(), beliefs=len(held))
        return {"beliefs": list(held), "answer_axis": "belief"}

    async def _compute_diff(self, state: HistoricalState) -> HistoricalState:
        """What changed over a period (FR-04.6)."""
        start = state["resolved_start"]
        end = state["resolved_end"]
        if end < start:
            start, end = end, start

        diff = await self._timeline.diff(start, end)
        _log.info(
            "historical_diff_computed",
            start=start.isoformat(),
            end=end.isoformat(),
            became_true=len(diff.became_true),
            ceased=len(diff.ceased_to_be_true),
            corrected=len(diff.corrected),
        )
        return {"diff": diff, "answer_axis": "range"}

    # --------------------------------------------------------------- internals

    @staticmethod
    def _route(state: HistoricalState) -> str:
        """The decision this workflow exists for.

        A range question goes to `diff` regardless of axis: "what changed" is
        inherently a world-time question, and `TimelineDiff` already separates
        "stopped being true" from "we were wrong about it" so the belief dimension is
        not lost by routing here.
        """
        interpretation = state["interpretation"]
        if interpretation.is_range:
            return "range"
        return "belief" if interpretation.axis == "belief" else "world"

    def _resolve_one(
        self, reference: TimeReference, anchor: datetime
    ) -> datetime | None:
        """One phrase to one instant, or None when it cannot be resolved.

        Returns None rather than a guess. ADR-010 leaves dates null instead of
        fabricating them, and a fabricated date on a question explicitly about a
        period would produce an answer about the wrong period with no signal.
        """
        if reference.kind != "clock_relative":
            return None

        descriptor = RelativeDescriptor(
            direction=TemporalDirection(reference.direction),
            quantity=reference.quantity,
            unit=TemporalUnit(reference.unit) if reference.unit else None,
            weekday=reference.weekday,
            modifier=(
                TemporalModifier(reference.modifier) if reference.modifier else None
            ),
        )
        start, _end, granularity = self._resolver.resolve(
            descriptor, anchor, self._clock.zone()
        )
        if granularity is Granularity.UNKNOWN or start is None:
            _log.info(
                "historical_time_unresolved", phrase=reference.raw_phrase or "(none)"
            )
            return None
        return start

    # ------------------------------------------------------------------ public

    async def run(self, query: HistoricalQuery) -> HistoricalAnalysisResult:
        final: HistoricalState = await self._graph.ainvoke({"query": query})

        interpretation = final.get("interpretation")
        unresolved: str | None = None
        if (
            interpretation is not None
            and query.start is None
            and interpretation.when.kind == "clock_relative"
            and self._resolve_one(interpretation.when, self._clock.now()) is None
        ):
            unresolved = interpretation.when.raw_phrase

        return HistoricalAnalysisResult(
            axis=final.get("answer_axis", "world"),
            is_range=final.get("answer_axis") == "range",
            when=final.get("resolved_start"),
            until=final.get("resolved_end"),
            world_facts=final.get("world_facts", []),
            beliefs=final.get("beliefs", []),
            diff=final.get("diff"),
            unresolved_phrase=unresolved,
        )

    async def compare_axes(
        self, when: datetime, limit: int = _DEFAULT_LIMIT
    ) -> object:
        """Both axes at one instant, for inspection and for the Unit 3 criterion.

        Exposed because "show me where these disagree" is the clearest demonstration
        that both axes are genuinely tracked, and it is the query a user would ask
        when they suspect the system has revised its memory.
        """
        return await self._timeline.compare(when, limit)
