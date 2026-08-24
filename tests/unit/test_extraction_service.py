"""Tests for the naive ExtractionService.

Focus is on the two contracts that would be expensive to get wrong later:

  - ADR-010: the model supplies structure, TimeResolver supplies dates, and an
    event-relative reference becomes an ordering constraint rather than a guess.
  - FR-02.7: origin is preserved exactly as extracted; an inference never becomes
    a user-stated fact.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from pca.domain.conversation import Episode
from pca.domain.enums import (
    Granularity,
    Origin,
    RelationDirection,
    ResolutionMethod,
)
from pca.domain.ids import ConversationId, EpisodeId
from pca.services.extraction import (
    ExtractedFact,
    ExtractionPayload,
    ExtractionService,
    TimeReference,
)
from pca.services.time_resolver import TimeResolver
from tests.fakes.llm import FakeLLMProvider

# Thursday 2026-01-01, 12:00 UTC
ANCHOR = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def episode(content: str = "text", zone: str = "Asia/Kolkata") -> Episode:
    return Episode(
        id=EpisodeId(uuid4()),
        content=content,
        occurred_at=ANCHOR,
        zone=zone,
        conversation_id=ConversationId(uuid4()),
    )


def service(payload: ExtractionPayload) -> ExtractionService:
    return ExtractionService(
        provider=FakeLLMProvider(structured_results=[payload]),
        resolver=TimeResolver(),
    )


async def test_clock_relative_reference_is_resolved_to_dates() -> None:
    payload = ExtractionPayload(
        facts=[
            ExtractedFact(
                statement="Argued with Priya",
                origin="user_stated",
                about=["Priya"],
                time_reference=TimeReference(
                    raw_phrase="last Tuesday",
                    kind="clock_relative",
                    direction="past",
                    weekday=1,
                    modifier="last",
                ),
            )
        ]
    )

    candidates = await service(payload).extract(episode())
    expression = candidates.facts[0].temporal_expression

    assert expression is not None
    assert expression.granularity is Granularity.DAY
    assert expression.method is ResolutionMethod.CLOCK_RELATIVE
    assert expression.resolved_from is not None
    # Local Tue 30 Dec in Asia/Kolkata == 2025-12-29T18:30Z
    assert expression.resolved_from == datetime(2025, 12, 29, 18, 30, tzinfo=UTC)


async def test_raw_phrase_is_always_retained() -> None:
    """ADR-010 requires the original phrase to survive resolution, so the
    resolution stays auditable and re-resolvable."""
    payload = ExtractionPayload(
        facts=[
            ExtractedFact(
                statement="Met her",
                origin="user_stated",
                time_reference=TimeReference(
                    raw_phrase="three weeks ago",
                    kind="clock_relative",
                    direction="past",
                    quantity=3,
                    unit="week",
                ),
            )
        ]
    )

    candidates = await service(payload).extract(episode())

    assert candidates.facts[0].temporal_expression.raw_phrase == "three weeks ago"


async def test_event_relative_reference_becomes_an_ordering_constraint() -> None:
    """'Before the wedding' cannot be dated, so no date is invented.

    A fabricated date is indistinguishable from a real one once stored, which is
    why this path yields a partial ordering instead.
    """
    payload = ExtractionPayload(
        facts=[
            ExtractedFact(
                statement="We stopped speaking",
                origin="user_stated",
                time_reference=TimeReference(
                    raw_phrase="before the wedding",
                    kind="event_relative",
                    reference_event="the wedding",
                    ordering="before",
                ),
            )
        ]
    )

    candidates = await service(payload).extract(episode())
    expression = candidates.facts[0].temporal_expression

    assert len(candidates.ordering_constraints) == 1
    constraint = candidates.ordering_constraints[0]
    assert constraint.direction is RelationDirection.BEFORE
    assert constraint.reference_phrase == "the wedding"

    assert expression is not None
    assert expression.granularity is Granularity.UNKNOWN
    assert expression.method is ResolutionMethod.UNRESOLVED
    assert expression.resolved_from is None


async def test_origin_is_preserved_exactly() -> None:
    """FR-02.7: an inference must never surface as something the user stated."""
    payload = ExtractionPayload(
        facts=[
            ExtractedFact(statement="She lives in Pune", origin="user_stated"),
            ExtractedFact(statement="They are close", origin="ai_inferred"),
        ]
    )

    candidates = await service(payload).extract(episode())

    assert candidates.facts[0].origin is Origin.USER_STATED
    assert candidates.facts[1].origin is Origin.AI_INFERRED


async def test_no_time_reference_yields_no_expression() -> None:
    payload = ExtractionPayload(
        facts=[ExtractedFact(statement="She works at Google", origin="user_stated")]
    )

    candidates = await service(payload).extract(episode())

    assert candidates.facts[0].temporal_expression is None
    assert candidates.ordering_constraints == []


async def test_resolution_uses_the_episode_zone_not_a_default() -> None:
    """The anchor zone must come from the episode.

    Extraction runs in the background, potentially on a machine or at a time with a
    different active zone. Resolving against anything but the captured zone shifts
    day boundaries.
    """
    payload = ExtractionPayload(
        facts=[
            ExtractedFact(
                statement="Saw her",
                origin="user_stated",
                time_reference=TimeReference(
                    raw_phrase="yesterday",
                    kind="clock_relative",
                    direction="past",
                    quantity=1,
                    unit="day",
                ),
            )
        ]
    )

    kolkata = await service(payload).extract(episode(zone="Asia/Kolkata"))

    payload_again = ExtractionPayload(facts=payload.facts)
    utc_zone = await service(payload_again).extract(episode(zone="UTC"))

    k = kolkata.facts[0].temporal_expression.resolved_from
    u = utc_zone.facts[0].temporal_expression.resolved_from
    assert k != u, "zone must affect the resolved window"
    assert k == datetime(2025, 12, 30, 18, 30, tzinfo=UTC)
    assert u == datetime(2025, 12, 31, 0, 0, tzinfo=UTC)


async def test_extraction_writes_nothing() -> None:
    """Extraction returns candidates only.

    Committing here would make conflict detection impossible, because there would
    be nothing to detect against before the write.
    """
    payload = ExtractionPayload(
        facts=[ExtractedFact(statement="A fact", origin="user_stated")]
    )
    provider = FakeLLMProvider(structured_results=[payload])
    extraction = ExtractionService(provider=provider, resolver=TimeResolver())

    candidates = await extraction.extract(episode())

    assert candidates.total == 1
    # The only interaction was the structured call; no repository or graph is
    # even reachable from this service.
    assert [c[0] for c in provider.calls] == ["structured"]


async def test_empty_extraction_is_valid() -> None:
    candidates = await service(ExtractionPayload(facts=[])).extract(episode())

    assert candidates.is_empty
    assert candidates.total == 0


async def test_candidates_carry_the_episode_id_for_provenance() -> None:
    """FR-02.5: every candidate must be traceable to its source."""
    source = episode()
    payload = ExtractionPayload(
        facts=[ExtractedFact(statement="A fact", origin="user_stated")]
    )

    candidates = await service(payload).extract(source)

    assert candidates.episode_id == source.id


@pytest.mark.parametrize(
    ("phrase", "quantity", "unit", "expected_granularity"),
    [
        ("yesterday", 1, "day", Granularity.DAY),
        ("three weeks ago", 3, "week", Granularity.WEEK),
        ("two months ago", 2, "month", Granularity.MONTH),
        ("last year", 1, "year", Granularity.YEAR),
    ],
)
async def test_granularity_matches_the_phrase_precision(
    phrase: str, quantity: int, unit: str, expected_granularity: Granularity
) -> None:
    """A vague phrase must not acquire fake precision."""
    payload = ExtractionPayload(
        facts=[
            ExtractedFact(
                statement="Something happened",
                origin="user_stated",
                time_reference=TimeReference(
                    raw_phrase=phrase,
                    kind="clock_relative",
                    direction="past",
                    quantity=quantity,
                    unit=unit,  # type: ignore[arg-type]
                ),
            )
        ]
    )

    candidates = await service(payload).extract(episode())

    assert candidates.facts[0].temporal_expression.granularity is expected_granularity


# ===========================================================================
# Unit 2 — full extraction: entities, events, relationships, salience
# ===========================================================================


def full_service(payload: ExtractionPayload) -> ExtractionService:
    from pca.services.salience import SalienceScorer

    return ExtractionService(
        provider=FakeLLMProvider(structured_results=[payload]),
        resolver=TimeResolver(),
        salience=SalienceScorer(),
    )


async def test_entities_are_extracted_with_types() -> None:
    from pca.domain.enums import EntityType
    from pca.services.extraction import ExtractedEntity

    payload = ExtractionPayload(
        entities=[
            ExtractedEntity(name="Suresh", entity_type="person"),
            ExtractedEntity(name="Google", entity_type="organization"),
            ExtractedEntity(name="Visakhapatnam", entity_type="place"),
        ]
    )

    candidates = await full_service(payload).extract(episode())

    by_name = {e.name: e.entity_type for e in candidates.entities}
    assert by_name["Suresh"] is EntityType.PERSON
    assert by_name["Google"] is EntityType.ORGANIZATION
    assert by_name["Visakhapatnam"] is EntityType.PLACE


async def test_relationships_are_extracted_and_normalised() -> None:
    """Relation types are normalised to lower_snake_case so "Works At" and
    "works_at" do not become two distinct relation kinds in the graph."""
    from pca.services.extraction import ExtractedRelationship

    payload = ExtractionPayload(
        relationships=[
            ExtractedRelationship(
                from_entity="me",
                to_entity="Suresh",
                relation_type="Close Friend",
                origin="user_stated",
            )
        ]
    )

    candidates = await full_service(payload).extract(episode())

    assert candidates.relationships[0].relation_type == "close_friend"


async def test_self_referential_relationship_is_dropped() -> None:
    """It is meaningless and would violate a database constraint, so it is dropped
    here rather than failing the whole commit later."""
    from pca.services.extraction import ExtractedRelationship

    payload = ExtractionPayload(
        relationships=[
            ExtractedRelationship(
                from_entity="Suresh",
                to_entity="suresh",
                relation_type="knows",
                origin="ai_inferred",
            )
        ]
    )

    candidates = await full_service(payload).extract(episode())

    assert candidates.relationships == []


async def test_events_are_extracted_with_participants_and_time() -> None:
    from pca.services.extraction import ExtractedEvent

    payload = ExtractionPayload(
        events=[
            ExtractedEvent(
                description="Argued about the house",
                origin="user_stated",
                category="significant_event",
                participants=["Priya"],
                time_reference=TimeReference(
                    raw_phrase="last Tuesday",
                    kind="clock_relative",
                    direction="past",
                    weekday=1,
                    modifier="last",
                ),
            )
        ]
    )

    candidates = await full_service(payload).extract(episode())
    event = candidates.events[0]

    assert event.participant_names == ["Priya"]
    assert event.temporal_expression.granularity is Granularity.DAY
    assert event.salience > 0.5


async def test_significant_events_score_higher_than_transient_detail() -> None:
    """The ADR-017 property that stops trivia burying signal."""
    payload = ExtractionPayload(
        facts=[
            ExtractedFact(
                statement="My sister got divorced",
                origin="user_stated",
                category="significant_event",
                about=["sister"],
            ),
            ExtractedFact(
                statement="I had toast",
                origin="user_stated",
                category="transient",
            ),
        ]
    )

    candidates = await full_service(payload).extract(episode())
    scores = {f.statement: f.salience for f in candidates.facts}

    assert scores["My sister got divorced"] > scores["I had toast"]


async def test_salience_category_is_carried_onto_the_candidate() -> None:
    """Persisted alongside the score so a future re-tuning can recompute rather than
    having to re-extract everything."""
    from pca.domain.enums import SalienceCategory

    payload = ExtractionPayload(
        facts=[
            ExtractedFact(
                statement="Suresh is a frontend developer",
                origin="user_stated",
                category="identity",
            )
        ]
    )

    candidates = await full_service(payload).extract(episode())

    assert candidates.facts[0].salience_category is SalienceCategory.IDENTITY


async def test_inferred_facts_score_below_stated_ones() -> None:
    payload = ExtractionPayload(
        facts=[
            ExtractedFact(
                statement="Suresh lives in Visakhapatnam",
                origin="user_stated",
                category="location",
            ),
            ExtractedFact(
                statement="Suresh probably enjoys the coast",
                origin="ai_inferred",
                category="location",
                confidence="uncertain",
            ),
        ]
    )

    candidates = await full_service(payload).extract(episode())
    scores = {f.statement: f.salience for f in candidates.facts}

    assert (
        scores["Suresh lives in Visakhapatnam"]
        > scores["Suresh probably enjoys the coast"]
    )


async def test_compound_statement_yields_separate_records() -> None:
    """The Unit 1b observation that motivated Unit 2.

    "My friend Suresh is a frontend developer in Visakhapatnam" carries a
    relationship, an occupation, and a location. Unit 1b's naive extraction captured
    only the location. All three must survive as separate records.
    """
    from pca.services.extraction import ExtractedEntity, ExtractedRelationship

    payload = ExtractionPayload(
        entities=[
            ExtractedEntity(name="Suresh", entity_type="person"),
            ExtractedEntity(name="Visakhapatnam", entity_type="place"),
        ],
        facts=[
            ExtractedFact(
                statement="Suresh is a frontend developer",
                origin="user_stated",
                category="identity",
                about=["Suresh"],
            ),
            ExtractedFact(
                statement="Suresh lives in Visakhapatnam",
                origin="user_stated",
                category="location",
                about=["Suresh", "Visakhapatnam"],
            ),
        ],
        relationships=[
            ExtractedRelationship(
                from_entity="me",
                to_entity="Suresh",
                relation_type="friend",
                origin="user_stated",
            )
        ],
    )

    candidates = await full_service(payload).extract(episode())

    statements = {f.statement for f in candidates.facts}
    assert "Suresh is a frontend developer" in statements
    assert "Suresh lives in Visakhapatnam" in statements
    assert candidates.relationships[0].relation_type == "friend"
    assert candidates.total >= 5


async def test_blank_entity_names_are_discarded() -> None:
    from pca.services.extraction import ExtractedEntity

    payload = ExtractionPayload(
        entities=[
            ExtractedEntity(name="  ", entity_type="person"),
            ExtractedEntity(name="Suresh", entity_type="person"),
        ]
    )

    candidates = await full_service(payload).extract(episode())

    assert [e.name for e in candidates.entities] == ["Suresh"]
