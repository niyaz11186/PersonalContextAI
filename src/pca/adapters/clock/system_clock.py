"""SystemClockAdapter — real wall clock.

Layer L5. Implements ClockPort.

The only place in the codebase permitted to read the system time. Boundary rule 4
forbids datetime.now() everywhere else, which is what makes temporal behaviour
testable.
"""

from datetime import UTC, datetime


class SystemClockAdapter:
    """Wall-clock time in UTC, with a configured IANA zone."""

    def __init__(self, zone: str) -> None:
        self._zone = zone

    def now(self) -> datetime:
        return datetime.now(UTC)

    def zone(self) -> str:
        return self._zone
