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
                people=["Priya"],
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
