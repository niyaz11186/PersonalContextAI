"""Exhaustive tests for TimeResolver (ADR-010, ADR-011).

This is the most important test file in Unit 1a. TimeResolver is where temporal
correctness is won or lost, and its failures are silent: a wrong date does not
raise, it just quietly corrupts the timeline and may go unnoticed for months.

The suite deliberately emphasises three things over breadth of phrasing:

  1. Local-zone arithmetic, not UTC. Off-by-one-day errors from computing day
     boundaries in UTC are the single most likely defect here.
  2. DST correctness. A day is 23 or 25 hours across a transition, not 24.
  3. The UNKNOWN invariant. Unresolvable must yield null dates, never a guess.

Reference anchor: 2026-01-01 is a Thursday.
"""

from datetime import UTC, datetime, timedelta

import pytest

from pca.domain.enums import (
    Granularity,
    ResolutionMethod,
    TemporalDirection,
    TemporalModifier,
    TemporalUnit,
)
from pca.domain.errors import TemporalResolutionError
from pca.domain.temporal import RelativeDescriptor, TemporalExpression
from pca.services.time_resolver import TimeResolver, add_months, quarter_of

# Thursday 2026-01-01, 12:00 UTC
ANCHOR = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

MONDAY, TUESDAY, WEDNESDAY, THURSDAY = 0, 1, 2, 3


@pytest.fixture
def resolver() -> TimeResolver:
    return TimeResolver()


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


# --------------------------------------------------------------------- weekdays


def test_last_tuesday_from_thursday(resolver: TimeResolver) -> None:
    """'Last Tuesday' is the most recent Tuesday strictly before the anchor."""
    descriptor = RelativeDescriptor(weekday=TUESDAY, modifier=TemporalModifier.LAST)
    start, end, granularity = resolver.resolve(descriptor, ANCHOR, "UTC")

    assert granularity is Granularity.DAY
    assert start == utc(2025, 12, 30)
    assert end == utc(2025, 12, 31)
    assert start is not None and start.weekday() == TUESDAY


def test_last_tuesday_when_anchor_is_tuesday_goes_back_a_full_week(
    resolver: TimeResolver,
) -> None:
    """The 'strictly before' rule matters most on the day itself.

    Saying "last Tuesday" on a Tuesday means the previous one, not today.
    Returning today would be an off-by-a-week error that looks plausible.
    """
    tuesday = datetime(2025, 12, 30, 9, 0, tzinfo=UTC)
    descriptor = RelativeDescriptor(weekday=TUESDAY, modifier=TemporalModifier.LAST)
    start, _, _ = resolver.resolve(descriptor, tuesday, "UTC")

    assert start == utc(2025, 12, 23)


def test_next_tuesday(resolver: TimeResolver) -> None:
    descriptor = RelativeDescriptor(weekday=TUESDAY, modifier=TemporalModifier.NEXT)
    start, end, granularity = resolver.resolve(descriptor, ANCHOR, "UTC")

    assert granularity is Granularity.DAY
    assert start == utc(2026, 1, 6)
    assert end == utc(2026, 1, 7)


def test_this_tuesday_uses_current_iso_week(resolver: TimeResolver) -> None:
    """'This Tuesday' is the Tuesday of the ISO week containing the anchor,
    which can fall before the anchor."""
    descriptor = RelativeDescriptor(weekday=TUESDAY, modifier=TemporalModifier.THIS)
    start, _, _ = resolver.resolve(descriptor, ANCHOR, "UTC")

    assert start == utc(2025, 12, 30)


@pytest.mark.parametrize("weekday", range(7))
def test_last_weekday_always_lands_on_that_weekday(
    resolver: TimeResolver, weekday: int
) -> None:
    descriptor = RelativeDescriptor(weekday=weekday, modifier=TemporalModifier.LAST)
    start, _, _ = resolver.resolve(descriptor, ANCHOR, "UTC")

    assert start is not None
    assert start.weekday() == weekday
    assert start < ANCHOR


# ---------------------------------------------------------------------- offsets


def test_three_weeks_ago_returns_week_granularity(resolver: TimeResolver) -> None:
    """'Three weeks ago' denotes a week, not an instant.

    Returning DAY precision here would assert precision the phrase does not carry.
    """
    descriptor = RelativeDescriptor(
        direction=TemporalDirection.PAST, quantity=3, unit=TemporalUnit.WEEK
    )
    start, end, granularity = resolver.resolve(descriptor, ANCHOR, "UTC")

    assert granularity is Granularity.WEEK
    assert start == utc(2025, 12, 8)   # Monday of that ISO week
    assert end == utc(2025, 12, 15)
    assert start is not None and start.weekday() == MONDAY


def test_yesterday(resolver: TimeResolver) -> None:
    descriptor = RelativeDescriptor(
        direction=TemporalDirection.PAST, quantity=1, unit=TemporalUnit.DAY
    )
    start, end, granularity = resolver.resolve(descriptor, ANCHOR, "UTC")

    assert granularity is Granularity.DAY
    assert start == utc(2025, 12, 31)
    assert end == utc(2026, 1, 1)


def test_three_months_ago(resolver: TimeResolver) -> None:
    descriptor = RelativeDescriptor(
        direction=TemporalDirection.PAST, quantity=3, unit=TemporalUnit.MONTH
    )
    start, end, granularity = resolver.resolve(descriptor, ANCHOR, "UTC")

    assert granularity is Granularity.MONTH
    assert start == utc(2025, 10, 1)
    assert end == utc(2025, 11, 1)


def test_two_years_ago(resolver: TimeResolver) -> None:
    descriptor = RelativeDescriptor(
        direction=TemporalDirection.PAST, quantity=2, unit=TemporalUnit.YEAR
    )
    start, end, granularity = resolver.resolve(descriptor, ANCHOR, "UTC")

    assert granularity is Granularity.YEAR
    assert start == utc(2024, 1, 1)
    assert end == utc(2025, 1, 1)


def test_future_direction(resolver: TimeResolver) -> None:
    descriptor = RelativeDescriptor(
        direction=TemporalDirection.FUTURE, quantity=2, unit=TemporalUnit.DAY
    )
    start, _, _ = resolver.resolve(descriptor, ANCHOR, "UTC")

    assert start == utc(2026, 1, 3)


# ------------------------------------------------------------ calendar periods


def test_last_month(resolver: TimeResolver) -> None:
    descriptor = RelativeDescriptor(
        modifier=TemporalModifier.LAST, unit=TemporalUnit.MONTH
    )
    start, end, granularity = resolver.resolve(descriptor, ANCHOR, "UTC")

    assert granularity is Granularity.MONTH
    assert start == utc(2025, 12, 1)
    assert end == utc(2026, 1, 1)


def test_this_year(resolver: TimeResolver) -> None:
    descriptor = RelativeDescriptor(
        modifier=TemporalModifier.THIS, unit=TemporalUnit.YEAR
    )
    start, end, granularity = resolver.resolve(descriptor, ANCHOR, "UTC")

    assert granularity is Granularity.YEAR
    assert start == utc(2026, 1, 1)
    assert end == utc(2027, 1, 1)


def test_last_quarter(resolver: TimeResolver) -> None:
    """From mid-Q2, 'last quarter' is the whole of Q1."""
    may = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    descriptor = RelativeDescriptor(
        modifier=TemporalModifier.LAST, unit=TemporalUnit.QUARTER
    )
    start, end, granularity = resolver.resolve(descriptor, may, "UTC")

    assert granularity is Granularity.QUARTER
    assert start == utc(2026, 1, 1)
    assert end == utc(2026, 4, 1)


# ------------------------------------------------------------------- timezones


def test_day_boundaries_use_local_zone_not_utc(resolver: TimeResolver) -> None:
    """The highest-value test in this file.

    Anchor is 01:00 UTC, which is already 06:30 the same day in Kolkata. If day
    arithmetic ran in UTC the result would be identical either way for this date,
    so the discriminating assertion is on the *boundary instants*: local midnight
    in Kolkata is 18:30 UTC the previous day.
    """
    anchor = datetime(2026, 3, 10, 1, 0, tzinfo=UTC)
    descriptor = RelativeDescriptor(
        direction=TemporalDirection.PAST, quantity=1, unit=TemporalUnit.DAY
    )
    start, end, _ = resolver.resolve(descriptor, anchor, "Asia/Kolkata")

    # Local 2026-03-09 00:00+05:30 == 2026-03-08T18:30Z
    assert start == utc(2026, 3, 8, 18, 30)
    assert end == utc(2026, 3, 9, 18, 30)
    assert end - start == timedelta(hours=24)


def test_local_date_differs_from_utc_date_across_midnight(
    resolver: TimeResolver,
) -> None:
    """At 20:00 UTC it is already the next day in Kolkata.

    'Yesterday' must therefore mean the UTC day itself, not the day before it.
    Getting this wrong shifts every imported or late-evening memory by a day.
    """
    anchor = datetime(2026, 6, 10, 20, 0, tzinfo=UTC)  # 2026-06-11 01:30 IST
    descriptor = RelativeDescriptor(
        direction=TemporalDirection.PAST, quantity=1, unit=TemporalUnit.DAY
    )
    start, _, _ = resolver.resolve(descriptor, anchor, "Asia/Kolkata")

    # Local anchor date is the 11th, so yesterday is the 10th local:
    # 2026-06-10 00:00+05:30 == 2026-06-09T18:30Z
    assert start == utc(2026, 6, 9, 18, 30)


def test_spring_forward_day_is_23_hours(resolver: TimeResolver) -> None:
    """DST correctness. US DST begins 2026-03-08, making that a 23-hour day.

    Building the window in local time and converting yields this naturally.
    Adding timedelta(days=1) to a UTC instant would produce a flat 24 hours and
    silently misplace anything near the boundary.
    """
    anchor = datetime(2026, 3, 9, 16, 0, tzinfo=UTC)  # 12:00 EDT on the 9th
    descriptor = RelativeDescriptor(
        direction=TemporalDirection.PAST, quantity=1, unit=TemporalUnit.DAY
    )
    start, end, _ = resolver.resolve(descriptor, anchor, "America/New_York")

    assert start == utc(2026, 3, 8, 5, 0)   # local midnight EST (UTC-5)
    assert end == utc(2026, 3, 9, 4, 0)     # local midnight EDT (UTC-4)
    assert end - start == timedelta(hours=23)


def test_fall_back_day_is_25_hours(resolver: TimeResolver) -> None:
    """US DST ends 2026-11-01, making that a 25-hour day."""
    anchor = datetime(2026, 11, 2, 17, 0, tzinfo=UTC)  # 12:00 EST on the 2nd
    descriptor = RelativeDescriptor(
        direction=TemporalDirection.PAST, quantity=1, unit=TemporalUnit.DAY
    )
    start, end, _ = resolver.resolve(descriptor, anchor, "America/New_York")

    assert end - start == timedelta(hours=25)


# ------------------------------------------------------- unresolvable handling


@pytest.mark.parametrize(
    "descriptor",
    [
        RelativeDescriptor(),
        RelativeDescriptor(direction=TemporalDirection.PAST),
        RelativeDescriptor(quantity=3),
        RelativeDescriptor(unit=TemporalUnit.WEEK),
        # quantity + unit but no direction: "three weeks" alone is not locatable
        RelativeDescriptor(quantity=3, unit=TemporalUnit.WEEK),
        # weekday without a modifier: "Tuesday" alone is ambiguous
        RelativeDescriptor(weekday=TUESDAY),
    ],
)
def test_insufficient_descriptor_yields_unknown_with_null_dates(
    resolver: TimeResolver, descriptor: RelativeDescriptor
) -> None:
    """Unresolvable must never produce a plausible-looking date.

    ADR-010: a fabricated date is worse than an honest absence, because it is
    indistinguishable from a real one once stored.
    """
    start, end, granularity = resolver.resolve(descriptor, ANCHOR, "UTC")

    assert granularity is Granularity.UNKNOWN
    assert start is None
    assert end is None


def test_naive_anchor_raises(resolver: TimeResolver) -> None:
    """A naive anchor is a programmer error, not user ambiguity, so it fails loudly."""
    descriptor = RelativeDescriptor(
        direction=TemporalDirection.PAST, quantity=1, unit=TemporalUnit.DAY
    )
    with pytest.raises(TemporalResolutionError, match="timezone-aware"):
        resolver.resolve(descriptor, datetime(2026, 1, 1, 12, 0), "UTC")


def test_unknown_zone_raises_with_actionable_message(resolver: TimeResolver) -> None:
    descriptor = RelativeDescriptor(
        direction=TemporalDirection.PAST, quantity=1, unit=TemporalUnit.DAY
    )
    with pytest.raises(TemporalResolutionError, match="tzdata"):
        resolver.resolve(descriptor, ANCHOR, "Mars/Olympus_Mons")


# ------------------------------------------------------------------ invariants


@pytest.mark.parametrize(
    "descriptor",
    [
        RelativeDescriptor(weekday=TUESDAY, modifier=TemporalModifier.LAST),
        RelativeDescriptor(
            direction=TemporalDirection.PAST, quantity=5, unit=TemporalUnit.WEEK
        ),
        RelativeDescriptor(modifier=TemporalModifier.LAST, unit=TemporalUnit.MONTH),
        RelativeDescriptor(modifier=TemporalModifier.THIS, unit=TemporalUnit.QUARTER),
        RelativeDescriptor(
            direction=TemporalDirection.FUTURE, quantity=1, unit=TemporalUnit.YEAR
        ),
    ],
)
def test_windows_are_half_open_and_ordered(
    resolver: TimeResolver, descriptor: RelativeDescriptor
) -> None:
    start, end, granularity = resolver.resolve(descriptor, ANCHOR, "Asia/Kolkata")

    assert start is not None and end is not None
    assert start < end
    assert granularity is not Granularity.UNKNOWN


def test_resolution_never_pairs_dates_with_unknown_granularity(
    resolver: TimeResolver,
) -> None:
    """Property check across a spread of descriptors.

    This is the invariant TemporalExpression also enforces at construction; both
    layers assert it because a violation here is unrecoverable once persisted.
    """
    descriptors = [
        RelativeDescriptor(),
        RelativeDescriptor(weekday=WEDNESDAY, modifier=TemporalModifier.NEXT),
        RelativeDescriptor(quantity=2, unit=TemporalUnit.QUARTER),
        RelativeDescriptor(
            direction=TemporalDirection.PAST, quantity=0, unit=TemporalUnit.DAY
        ),
    ]
    for descriptor in descriptors:
        start, end, granularity = resolver.resolve(descriptor, ANCHOR, "UTC")
        if granularity is Granularity.UNKNOWN:
            assert start is None and end is None
        else:
            assert start is not None and end is not None


# --------------------------------------------------------- calendar arithmetic


def test_add_months_clamps_short_month() -> None:
    """31 March minus one month is 28 February 2026, not 3 March.

    Clamping is what people mean by "last month"; overflowing into the next month
    would place the memory in the wrong period entirely.
    """
    from datetime import date

    assert add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)


def test_add_months_handles_leap_year() -> None:
    from datetime import date

    assert add_months(date(2024, 3, 31), -1) == date(2024, 2, 29)


def test_add_months_crosses_year_boundaries() -> None:
    from datetime import date

    assert add_months(date(2026, 1, 15), -1) == date(2025, 12, 15)
    assert add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
    assert add_months(date(2026, 6, 15), -18) == date(2024, 12, 15)


def test_leap_day_year_arithmetic_clamps() -> None:
    """29 February minus one year has no exact counterpart; clamp rather than raise."""
    resolver = TimeResolver()
    leap_day = datetime(2024, 2, 29, 12, 0, tzinfo=UTC)
    descriptor = RelativeDescriptor(
        direction=TemporalDirection.PAST, quantity=1, unit=TemporalUnit.YEAR
    )
    start, end, granularity = resolver.resolve(descriptor, leap_day, "UTC")

    assert granularity is Granularity.YEAR
    assert start == utc(2023, 1, 1)
    assert end == utc(2024, 1, 1)


@pytest.mark.parametrize(
    ("month", "expected"),
    [(1, 1), (3, 1), (4, 2), (6, 2), (7, 3), (9, 3), (10, 4), (12, 4)],
)
def test_quarter_of(month: int, expected: int) -> None:
    from datetime import date

    assert quarter_of(date(2026, month, 1)) == expected


# ------------------------------------------- TemporalExpression own invariants


def test_temporal_expression_rejects_dates_with_unknown_granularity() -> None:
    with pytest.raises(ValueError, match="UNKNOWN granularity"):
        TemporalExpression(
            raw_phrase="sometime around then",
            granularity=Granularity.UNKNOWN,
            method=ResolutionMethod.UNRESOLVED,
            anchor_zone="UTC",
            resolved_from=ANCHOR,
        )


def test_temporal_expression_rejects_unresolved_with_real_granularity() -> None:
    with pytest.raises(ValueError, match="UNRESOLVED method"):
        TemporalExpression(
            raw_phrase="before the wedding",
            granularity=Granularity.DAY,
            method=ResolutionMethod.UNRESOLVED,
            anchor_zone="UTC",
        )


def test_temporal_expression_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        TemporalExpression(
            raw_phrase="last Tuesday",
            granularity=Granularity.DAY,
            method=ResolutionMethod.CLOCK_RELATIVE,
            anchor_zone="UTC",
            resolved_from=datetime(2025, 12, 30),
        )


def test_temporal_expression_retains_raw_phrase() -> None:
    """ADR-010 requires the original phrase to survive resolution.

    It is what makes the resolution auditable and re-resolvable later.
    """
    expression = TemporalExpression(
        raw_phrase="last Tuesday",
        granularity=Granularity.DAY,
        method=ResolutionMethod.CLOCK_RELATIVE,
        anchor_zone="Asia/Kolkata",
        resolved_from=utc(2025, 12, 29, 18, 30),
        resolved_to=utc(2025, 12, 30, 18, 30),
    )

    assert expression.raw_phrase == "last Tuesday"
    assert expression.anchor_zone == "Asia/Kolkata"
    assert expression.is_resolved
