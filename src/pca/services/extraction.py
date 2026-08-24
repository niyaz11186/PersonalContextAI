"""ExtractionService — full extraction (Unit 2).

Layer L3.

Extracts facts, events, entities, and relationships from an episode, resolves time
references, and classifies salience. Returns **candidates only** — it writes nothing.

That separation is load-bearing. Committing here would make conflict detection
impossible, because there would be nothing to detect against before the write.
MemoryService owns commitment; ConflictDetectionService (Unit 3) runs in between.

Two divisions of labour are preserved throughout, both following the same principle
of letting the model classify and letting code compute:

    ADR-010  model returns the STRUCTURE of a time phrase; TimeResolver computes dates
    ADR-017  model returns a salience CATEGORY; SalienceScorer computes the number

In both cases asking the model for the final value directly produces figures that
drift between identical calls, cannot be tuned coherently, and cannot be explained
when a result looks wrong.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from pca.domain.conversation import Episode
from pca.domain.enums import (
    Confidence,
    EntityType,
    Granularity,
    Origin,
    RelationDirection,
    ResolutionMethod,
    SalienceCategory,
    TemporalDirection,
    TemporalModifier,
    TemporalUnit,
)
from pca.domain.extraction import (
    CandidateEntity,
    CandidateEvent,
    CandidateFact,
    CandidateRelationship,
    ExtractionCandidates,
)
from pca.domain.temporal import (
    OrderingConstraint,
    RelativeDescriptor,
    TemporalExpression,
)
from pca.observability.logging import get_logger
from pca.ports.llm import LLMProviderPort, Prompt, PromptMessage
from pca.services.salience import SalienceScorer
from pca.services.time_resolver import TimeResolver

_log = get_logger(__name__)

_SYSTEM_PROMPT = """\
You extract durable personal context from a message. Be thorough.

Extract four things:

1. ENTITIES — every person, organization, place, or project named or clearly implied.
2. FACTS — statements about the world, including who someone is, what they do,
   where they live, what they prefer, and what changed.
3. EVENTS — things that happened at a point or over a period.
4. RELATIONSHIPS — how entities relate to each other (sibling, friend, colleague,
   employer, resident_of, works_at, and so on). Use lower_snake_case relation types.

Rules you must follow:

- ORIGIN. Mark "user_stated" only for what the user actually asserted. Anything you
  infer, however reasonable, must be "ai_inferred". Never blur the two.
- TIME. For any time reference, return only its STRUCTURE. Never compute or return a
  date, and never guess a year. If a reference depends on another event (for example
  "before the wedding"), report it as event_relative rather than as an offset.
- CATEGORY. Classify each fact and event by what kind of information it carries. Do
  not return a score or a priority; the category is all that is needed.
- SPLIT COMPOUND STATEMENTS. "My friend Suresh is a frontend developer in
  Visakhapatnam" contains a relationship (friend), an occupation, and a location.
  Extract each separately rather than as one blob.
- DO NOT INVENT. No people, places, dates, or details that are not present or
  directly implied.
- Prefer including a borderline item over dropping it.
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


_Category = Literal[
    "significant_event",
    "relationship",
    "decision",
    "commitment",
    "state_change",
    "identity",
    "location",
    "preference",
    "transient",
]

_EntityKind = Literal["person", "organization", "place", "project", "other"]
_Origin = Literal["user_stated", "ai_inferred"]


class ExtractedEntity(BaseModel):
    name: str
    entity_type: _EntityKind
    aliases: list[str] = Field(default_factory=list)


class ExtractedFact(BaseModel):
    statement: str
    origin: _Origin
    confidence: Literal["certain", "probable", "uncertain"] = "probable"
    category: _Category = "identity"
    about: list[str] = Field(
        default_factory=list, description="names of entities this fact concerns"
    )
    time_reference: TimeReference | None = None


class ExtractedEvent(BaseModel):
    description: str
    origin: _Origin
    category: _Category = "significant_event"
    participants: list[str] = Field(default_factory=list)
    time_reference: TimeReference | None = None


class ExtractedRelationship(BaseModel):
    from_entity: str
    to_entity: str
    relation_type: str = Field(description="lower_snake_case, e.g. sibling, works_at")
    origin: _Origin


class ExtractionPayload(BaseModel):
    entities: list[ExtractedEntity] = Field(default_factory=list)
    facts: list[ExtractedFact] = Field(default_factory=list)
    events: list[ExtractedEvent] = Field(default_factory=list)
    relationships: list[ExtractedRelationship] = Field(default_factory=list)


class ExtractionService:
    def __init__(
        self,
        provider: LLMProviderPort,
        resolver: TimeResolver,
        salience: SalienceScorer | None = None,
        model: str | None = None,
    ) -> None:
        self._provider = provider
        self._resolver = resolver
        self._salience = salience or SalienceScorer()
        self._model = model

    async def extract(self, episode: Episode) -> ExtractionCandidates:
        payload = await self._provider.structured(
            Prompt(
                system=_SYSTEM_PROMPT,
                messages=[PromptMessage(role="user", content=episode.content)],
                temperature=0.1,
            ),
            ExtractionPayload,
            model=self._model,
        )

        constraints: list[OrderingConstraint] = []

        entities = [
            CandidateEntity(
                name=e.name.strip(),
                entity_type=EntityType(e.entity_type),
                aliases=[a.strip() for a in e.aliases if a.strip()],
            )
            for e in payload.entities
            if e.name.strip()
        ]

        facts: list[CandidateFact] = []
        for extracted in payload.facts:
            expression, constraint = self._resolve_time(extracted.time_reference, episode)
            if constraint:
                constraints.append(constraint)

            origin = Origin(extracted.origin)
            confidence = Confidence(extracted.confidence)
            category = SalienceCategory(extracted.category)
            facts.append(
                CandidateFact(
                    statement=extracted.statement,
                    origin=origin,
                    confidence=confidence,
                    salience=self._salience.score(
                        category=category,
                        origin=origin,
                        confidence=confidence,
                        involves_entities=bool(extracted.about),
                        is_temporally_anchored=self._is_anchored(expression),
                    ),
                    salience_category=category,
                    subject_names=[n.strip() for n in extracted.about if n.strip()],
                    temporal_expression=expression,
                )
            )

        events: list[CandidateEvent] = []
        for extracted_event in payload.events:
            expression, constraint = self._resolve_time(
                extracted_event.time_reference, episode
            )
            if constraint:
                constraints.append(constraint)

            origin = Origin(extracted_event.origin)
            category = SalienceCategory(extracted_event.category)
            events.append(
                CandidateEvent(
                    description=extracted_event.description,
                    origin=origin,
                    participant_names=[
                        n.strip() for n in extracted_event.participants if n.strip()
                    ],
                    temporal_expression=expression,
                    salience=self._salience.score(
                        category=category,
                        origin=origin,
                        involves_entities=bool(extracted_event.participants),
                        is_temporally_anchored=self._is_anchored(expression),
                    ),
                    salience_category=category,
                )
            )

        relationships = [
            CandidateRelationship(
                from_name=r.from_entity.strip(),
                to_name=r.to_entity.strip(),
                relation_type=r.relation_type.strip().lower().replace(" ", "_"),
                origin=Origin(r.origin),
            )
            for r in payload.relationships
            if r.from_entity.strip()
            and r.to_entity.strip()
            # A self-referential relationship is meaningless and violates a database
            # constraint, so it is dropped here rather than failing the whole commit.
            and r.from_entity.strip().casefold() != r.to_entity.strip().casefold()
        ]

        candidates = ExtractionCandidates(
            episode_id=episode.id,
            facts=facts,
            events=events,
            entities=entities,
            relationships=relationships,
            ordering_constraints=constraints,
        )

        _log.info(
            "extraction_complete",
            episode_id=str(episode.id),
            entities=len(entities),
            facts=len(facts),
            events=len(events),
            relationships=len(relationships),
            ordering_constraints=len(constraints),
            unresolved_times=sum(
                1
                for f in facts
                if f.temporal_expression
                and f.temporal_expression.granularity is Granularity.UNKNOWN
            ),
            top_salience=max((f.salience for f in facts), default=0.0),
        )
        return candidates

    # --------------------------------------------------------------- internals

    @staticmethod
    def _is_anchored(expression: TemporalExpression | None) -> bool:
        return bool(
            expression
            and expression.granularity is not Granularity.UNKNOWN
            and expression.resolved_from is not None
        )

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
