"""ExtractionService — naive version for Unit 1b.

Layer L3.

Scope: extracts plain facts plus resolved time references. Entity resolution,
salience scoring, relationships, and conflict detection are Unit 2 and Unit 3.

One deliberate addition beyond the "naive" plan: **temporal resolution is wired
in here rather than deferred to Unit 2.** TimeResolver is already built and proven
(53 tests, live contract verified), and every message extracted without it would
carry an unanchored date that Unit 2 could not retroactively repair — the message
anchor is available now and lost later. The cost is one extra field in the LLM
schema; the alternative is knowingly writing wrong dates for the duration of Unit 1b.

The ADR-010 split is preserved exactly: the model returns the *structure* of a
time phrase and never a date. TimeResolver computes dates deterministically.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from pca.domain.conversation import Episode
from pca.domain.enums import (
    Confidence,
    Granularity,
    Origin,
    RelationDirection,
    ResolutionMethod,
    TemporalDirection,
    TemporalModifier,
    TemporalUnit,
)
from pca.domain.extraction import CandidateFact, ExtractionCandidates
from pca.domain.temporal import OrderingConstraint, RelativeDescriptor, TemporalExpression
from pca.observability.logging import get_logger
from pca.ports.llm import LLMProviderPort, Prompt, PromptMessage
from pca.services.time_resolver import TimeResolver

_log = get_logger(__name__)

_SYSTEM_PROMPT = """\
You extract durable personal context from a message.

Return facts the user has stated or that follow directly from what they said.

Rules you must follow:
- Mark origin as "user_stated" only for things the user actually asserted.
  Anything you infer must be marked "ai_inferred". Never blur the two.
- For any time reference, return only its STRUCTURE. Never compute or return a
  date, and never guess a year. If the reference depends on another event
  (for example "before the wedding"), report it as an event-relative reference
  instead of a structured offset.
- Extract aggressively: prefer including a borderline fact over dropping it.
- Do not invent people, places, or details that are not present in the message.
"""


class TimeReference(BaseModel):
    """Structure of a time phrase. Contains no date, by design (ADR-010)."""

    raw_phrase: str = Field(description="the exact phrase as it appears in the message")
    kind: Literal["clock_relative", "event_relative", "none"] = Field(
        description="clock_relative for offsets like 'three weeks ago'; "
        "event_relative for references like 'before the wedding'"
    )
    direction: Literal["past", "future", "none"] = "none"
    quantity: int | None = None
    unit: Literal["day", "week", "month", "quarter", "year"] | None = None
    weekday: int | None = Field(default=None, description="0=Monday..6=Sunday, null if absent")
    modifier: Literal["last", "this", "next"] | None = None
    reference_event: str | None = Field(
        default=None, description="for event_relative: the event referred to"
    )
    ordering: Literal["before", "after"] | None = None


class ExtractedFact(BaseModel):
    statement: str
    origin: Literal["user_stated", "ai_inferred"]
    confidence: Literal["certain", "probable", "uncertain"] = "probable"
    people: list[str] = Field(default_factory=list)
    time_reference: TimeReference | None = None


class ExtractionPayload(BaseModel):
    facts: list[ExtractedFact] = Field(default_factory=list)


class ExtractionService:
    def __init__(
        self,
        provider: LLMProviderPort,
        resolver: TimeResolver,
        model: str | None = None,
    ) -> None:
        self._provider = provider
        self._resolver = resolver
        self._model = model

    async def extract(self, episode: Episode) -> ExtractionCandidates:
        """Produce candidates from an episode. Writes nothing.

        Returning candidates rather than committing is what allows conflict
        detection to run in between (Unit 3). An extraction that wrote directly
        would make contradiction handling impossible.
        """
        payload = await self._provider.structured(
            Prompt(
                system=_SYSTEM_PROMPT,
                messages=[PromptMessage(role="user", content=episode.content)],
                temperature=0.1,
            ),
            ExtractionPayload,
            model=self._model,
        )

        facts: list[CandidateFact] = []
        constraints: list[OrderingConstraint] = []

        for extracted in payload.facts:
            expression, constraint = self._resolve_time(
                extracted.time_reference, episode
            )
            if constraint:
                constraints.append(constraint)
            facts.append(
                CandidateFact(
                    statement=extracted.statement,
                    origin=Origin(extracted.origin),
                    confidence=Confidence(extracted.confidence),
                    subject_names=extracted.people,
                    temporal_expression=expression,
                )
            )

        candidates = ExtractionCandidates(
            episode_id=episode.id,
            facts=facts,
            ordering_constraints=constraints,
        )
        _log.info(
            "extraction_complete",
            episode_id=str(episode.id),
            facts=len(facts),
            ordering_constraints=len(constraints),
            unresolved_times=sum(
                1
                for f in facts
                if f.temporal_expression
                and f.temporal_expression.granularity is Granularity.UNKNOWN
            ),
        )
        return candidates

    # --------------------------------------------------------------- internals

    def _resolve_time(
        self, reference: TimeReference | None, episode: Episode
    ) -> tuple[TemporalExpression | None, OrderingConstraint | None]:
        """Turn a model-supplied structure into a resolved expression.

        Three outcomes, matching ADR-010:
          - clock-relative and resolvable -> dated expression
          - event-relative               -> ordering constraint, no date invented
          - anything else                -> UNKNOWN granularity with null dates
        """
        if reference is None or reference.kind == "none":
            return None, None

        if reference.kind == "event_relative":
            constraint = OrderingConstraint(
                raw_phrase=reference.raw_phrase,
                direction=RelationDirection(reference.ordering or "before"),
                reference_phrase=reference.reference_event or "",
            )
            # Deliberately UNRESOLVED rather than a plausible date. A fabricated
            # date is indistinguishable from a real one once stored.
            return (
                TemporalExpression(
                    raw_phrase=reference.raw_phrase,
                    granularity=Granularity.UNKNOWN,
                    method=ResolutionMethod.UNRESOLVED,
                    anchor_zone=episode.zone,
                ),
                constraint,
            )

        descriptor = RelativeDescriptor(
            direction=TemporalDirection(reference.direction),
            quantity=reference.quantity,
            unit=TemporalUnit(reference.unit) if reference.unit else None,
            weekday=reference.weekday,
            modifier=TemporalModifier(reference.modifier) if reference.modifier else None,
        )

        start, end, granularity = self._resolver.resolve(
            descriptor, episode.occurred_at, episode.zone
        )

        return (
            TemporalExpression(
                raw_phrase=reference.raw_phrase,
                descriptor=descriptor,
                resolved_from=start,
                resolved_to=end,
                granularity=granularity,
                method=(
                    ResolutionMethod.CLOCK_RELATIVE
                    if granularity is not Granularity.UNKNOWN
                    else ResolutionMethod.UNRESOLVED
                ),
                anchor_zone=episode.zone,
            ),
            None,
        )
