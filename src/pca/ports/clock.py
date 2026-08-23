"""ClockPort — the single source of "now".

Layer L4.

This looks like over-abstraction until you try to test bi-temporal correctness.
Verifying "what was true in March" against "what did I think in March" requires
scripting the passage of months, and that is impossible if any component calls
datetime.now() directly. Boundary rule 4 forbids that call anywhere in the
codebase; every timestamp flows from here.

Also the first of the three evaluation seams in ADR-016.
"""

from datetime import datetime
from typing import Protocol


class ClockPort(Protocol):
    """Current time and the active timezone."""

    def now(self) -> datetime:
        """Timezone-aware UTC instant. Never naive."""
        ...

    def zone(self) -> str:
        """Active IANA zone name, e.g. "Asia/Kolkata" (ADR-011)."""
        ...
