"""Scriptable clock — the first evaluation seam (ADR-016).

Lets a test advance months in microseconds, which is what makes the core product
hypothesis testable at all: "state a fact, let three months pass, ask a related
question, assert the fact was retrieved."
"""

from datetime import UTC, datetime, timedelta


class FakeClock:
    """Controllable implementation of ClockPort."""

    def __init__(self, start: datetime | None = None, zone: str = "UTC") -> None:
        if start is None:
            start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        if start.tzinfo is None:
            raise ValueError("FakeClock requires a timezone-aware start")
        self._now = start.astimezone(UTC)
        self._zone = zone

    def now(self) -> datetime:
        return self._now

    def zone(self) -> str:
        return self._zone

    # ------------------------------------------------------------- test control

    def advance(self, **kwargs: float) -> datetime:
        """Move forward, e.g. advance(days=90)."""
        self._now = self._now + timedelta(**kwargs)
        return self._now

    def set(self, when: datetime) -> datetime:
        if when.tzinfo is None:
            raise ValueError("set() requires a timezone-aware datetime")
        self._now = when.astimezone(UTC)
        return self._now

    def set_zone(self, zone: str) -> None:
        self._zone = zone
