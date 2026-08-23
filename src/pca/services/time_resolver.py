"""Deterministic resolution of relative time references (ADR-010, ADR-011).

This module is pure: no I/O, no model calls, no ports. That is deliberate.

ADR-010 splits the work of understanding a phrase like "three weeks ago":

    Gemini      identifies the phrase and its structure -> RelativeDescriptor
    TimeResolver computes the actual dates             -> this module

The split exists because language models are reliable at spotting time
expressions and unreliable at date arithmetic, and their arithmetic mistakes are
silent. A wrong date does not raise; it just quietly corrupts the timeline and
may go unnoticed for months. Deterministic arithmetic here is exhaustively
testable, which is where that risk is actually retired.

Two invariants hold throughout:

1. **All calendar arithmetic runs in the user's local zone**, never UTC.
   "Last Tuesday" means a local calendar day. Computing day boundaries in UTC
   shifts them by the zone offset and silently produces off-by-one-day errors.

2. **Unresolvable means UNKNOWN with null dates**, never a plausible guess.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pca.domain.enums import (
    Granularity,
    TemporalDirection,
    TemporalModifier,
    TemporalUnit,
)
from pca.domain.errors import TemporalResolutionError
from pca.domain.temporal import RelativeDescriptor

_UNIT_GRANULARITY: dict[TemporalUnit, Granularity] = {
    TemporalUnit.DAY: Granularity.DAY,
    TemporalUnit.WEEK: Granularity.WEEK,
    TemporalUnit.MONTH: Granularity.MONTH,
    TemporalUnit.QUARTER: Granularity.QUARTER,
    TemporalUnit.YEAR: Granularity.YEAR,
}

_UNRESOLVED: tuple[None, None, Granularity] = (None, None, Granularity.UNKNOWN)

type Resolution = tuple[datetime | None, datetime | None, Granularity]


def add_months(anchor: date, months: int) -> date:
    """Shift by whole calendar months, clamping the day to the target month.

    31 January minus one month is 28 or 29 February, not an error and not 3 March.
    Clamping is the behaviour people mean when they say "last month".
    """
    total = (anchor.year * 12 + (anchor.month - 1)) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(anchor.day, monthrange(year, month)[1])
    return date(year, month, day)


def quarter_of(anchor: date) -> int:
    """1..4."""
    return (anchor.month - 1) // 3 + 1


class TimeResolver:
    """Resolves RelativeDescriptor into a concrete half-open UTC window.

    Stateless and safe to share.
    """

    def resolve(
        self,
        descriptor: RelativeDescriptor,
        anchor: datetime,
        zone: str,
    ) -> Resolution:
        """Resolve `descriptor` relative to `anchor`, in `zone`.

        Args:
            descriptor: structured time reference produced by extraction.
            anchor: the message's capture instant. Must be timezone-aware.
            zone: IANA zone name active at capture.

        Returns:
            (from_utc, to_utc, granularity) with a half-open window
            [from_utc, to_utc). Returns (None, None, UNKNOWN) when the descriptor
            carries insufficient information — never a guess.

        Raises:
            TemporalResolutionError: if `anchor` is naive or `zone` is unknown.
                These are programmer errors, not user-input ambiguity, so they
                fail loudly rather than degrading to UNKNOWN.
        """
        if anchor.tzinfo is None:
            raise TemporalResolutionError("anchor must be timezone-aware")

        tz = self._zone(zone)
        local_date = anchor.astimezone(tz).date()

        if not descriptor.is_resolvable:
            return _UNRESOLVED

        # Weekday references are checked first: "last Tuesday" carries both a
        # modifier and a weekday, and the weekday is the more specific signal.
        if descriptor.weekday is not None and descriptor.modifier is not None:
            target = self._resolve_weekday(local_date, descriptor.weekday, descriptor.modifier)
            return self._window(target, Granularity.DAY, tz)

        if (
            descriptor.quantity is not None
            and descriptor.unit is not None
            and descriptor.direction is not TemporalDirection.NONE
        ):
            target = self._shift(local_date, descriptor.quantity, descriptor.unit, descriptor.direction)
            return self._window(target, _UNIT_GRANULARITY[descriptor.unit], tz)

        if descriptor.modifier is not None and descriptor.unit is not None:
            steps = {
                TemporalModifier.LAST: -1,
                TemporalModifier.THIS: 0,
                TemporalModifier.NEXT: 1,
            }[descriptor.modifier]
            direction = TemporalDirection.PAST if steps < 0 else TemporalDirection.FUTURE
            target = self._shift(local_date, abs(steps), descriptor.unit, direction)
            return self._window(target, _UNIT_GRANULARITY[descriptor.unit], tz)

        return _UNRESOLVED

    # ---------------------------------------------------------------- internals

    @staticmethod
    def _zone(zone: str) -> ZoneInfo:
        try:
            return ZoneInfo(zone)
        except ZoneInfoNotFoundError as exc:
            # On Windows this most often means the `tzdata` package is missing:
            # the OS ships no IANA database, so zoneinfo has nothing to read.
            raise TemporalResolutionError(
                f"unknown IANA timezone {zone!r}; on Windows ensure `tzdata` is installed"
            ) from exc

    @staticmethod
    def _resolve_weekday(anchor: date, weekday: int, modifier: TemporalModifier) -> date:
        """Find the referenced occurrence of a weekday.

        "Last Tuesday" is read as the most recent Tuesday strictly before the
        anchor day. English also permits "Tuesday of last week"; the stricter
        reading is chosen because it is the more common intent and, critically,
        because it is deterministic. The raw phrase is retained on the
        TemporalExpression either way, so a different reading remains recoverable.
        """
        match modifier:
            case TemporalModifier.LAST:
                delta = (anchor.weekday() - weekday) % 7 or 7
                return anchor - timedelta(days=delta)
            case TemporalModifier.NEXT:
                delta = (weekday - anchor.weekday()) % 7 or 7
                return anchor + timedelta(days=delta)
            case TemporalModifier.THIS:
                week_start = anchor - timedelta(days=anchor.weekday())
                return week_start + timedelta(days=weekday)

    @staticmethod
    def _shift(
        anchor: date,
        quantity: int,
        unit: TemporalUnit,
        direction: TemporalDirection,
    ) -> date:
        sign = -1 if direction is TemporalDirection.PAST else 1
        match unit:
            case TemporalUnit.DAY:
                return anchor + timedelta(days=sign * quantity)
            case TemporalUnit.WEEK:
                return anchor + timedelta(weeks=sign * quantity)
            case TemporalUnit.MONTH:
                return add_months(anchor, sign * quantity)
            case TemporalUnit.QUARTER:
                return add_months(anchor, sign * quantity * 3)
            case TemporalUnit.YEAR:
                # Via months so that 29 February clamps rather than raising.
                return add_months(anchor, sign * quantity * 12)

    def _window(self, target: date, granularity: Granularity, tz: ZoneInfo) -> Resolution:
        """Expand a target date into the local window for its granularity.

        The window is the whole period the phrase denotes, not a single instant.
        "Three weeks ago" means that week, so returning a point timestamp would
        assert precision the phrase does not carry.
        """
        match granularity:
            case Granularity.DAY:
                start, end = target, target + timedelta(days=1)
            case Granularity.WEEK:
                start = target - timedelta(days=target.weekday())  # ISO: Monday
                end = start + timedelta(days=7)
            case Granularity.MONTH:
                start = target.replace(day=1)
                end = add_months(start, 1)
            case Granularity.QUARTER:
                first_month = (quarter_of(target) - 1) * 3 + 1
                start = date(target.year, first_month, 1)
                end = add_months(start, 3)
            case Granularity.YEAR:
                start = date(target.year, 1, 1)
                end = date(target.year + 1, 1, 1)
            case _:
                return _UNRESOLVED

        return self._to_utc(start, tz), self._to_utc(end, tz), granularity

    @staticmethod
    def _to_utc(local_day: date, tz: ZoneInfo) -> datetime:
        """Local midnight on `local_day`, expressed as a UTC instant.

        Because the boundary is built in local time and then converted, a day
        spanning a DST transition is correctly 23 or 25 hours long rather than a
        fixed 24.

        Edge case, documented rather than handled: a few zones transition at
        midnight itself, making local midnight ambiguous or nonexistent. zoneinfo
        resolves these via `fold`, defaulting to the earlier offset. The
        resulting instant can be off by the DST delta on those specific days in
        those specific zones. Accepted for now; the alternative is materially
        more complexity for a rare case, and day-granularity queries tolerate it.
        """
        naive_midnight = datetime(local_day.year, local_day.month, local_day.day)
        return naive_midnight.replace(tzinfo=tz).astimezone(UTC)
