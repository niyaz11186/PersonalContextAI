"""Temporal value types.

Layer L0. Standard library only.

The central idea in this module is that the system tracks **two independent time
axes**, and conflating them is how a temporal memory system starts giving
confidently wrong answers:

    TemporalValidity  -> when a fact was true in the world
    BeliefWindow      -> when the system believed the fact

"What was true in March?" reads the first axis. "What did I think was true in
March?" reads the second. Those are different questions with different answers,
and satisfying FR-04.5 and FR-05.5 simultaneously requires keeping them apart.
"""

from dataclasses import dataclass
from datetime import datetime

from pca.domain.enums import (
    Granularity,
    RelationDirection,
    ResolutionMethod,
    TemporalDirection,
    TemporalModifier,
    TemporalUnit,
)


def _assert_aware(value: datetime | None, field: str) -> None:
    """Reject naive datetimes at the boundary.

    Every timestamp in this system is a timezone-aware UTC instant (ADR-011).
    A naive datetime here would silently corrupt ordering later, so it is
    refused at construction rather than tolerated.
    """
    if value is not None and value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware; got a naive datetime")


@dataclass(frozen=True, slots=True)
class TemporalValidity:
    """When a fact was true in the world.

    valid_to of None means "still true as far as we know".
    """

    valid_from: datetime | None = None
    valid_to: datetime | None = None

    def __post_init__(self) -> None:
        _assert_aware(self.valid_from, "valid_from")
        _assert_aware(self.valid_to, "valid_to")
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")

    def covers(self, when: datetime) -> bool:
        """Whether the fact was true in the world at `when`."""
        _assert_aware(when, "when")
        if self.valid_from and when < self.valid_from:
            return False
        if self.valid_to and when >= self.valid_to:
            return False
        return True


@dataclass(frozen=True, slots=True)
class BeliefWindow:
    """When the system believed a fact. Distinct from world-time validity.

    retracted_at of None means "still believed".
    """

    asserted_at: datetime
    retracted_at: datetime | None = None

    def __post_init__(self) -> None:
        _assert_aware(self.asserted_at, "asserted_at")
        _assert_aware(self.retracted_at, "retracted_at")
        if self.retracted_at and self.retracted_at < self.asserted_at:
            raise ValueError("retracted_at must not precede asserted_at")

    def held_at(self, when: datetime) -> bool:
        """Whether the system believed this at `when`.

        Deliberately mirrors TemporalValidity.covers so the two axes read the
        same way at call sites while remaining separate data.
        """
        _assert_aware(when, "when")
        if when < self.asserted_at:
            return False
        if self.retracted_at and when >= self.retracted_at:
            return False
        return True


@dataclass(frozen=True, slots=True)
class RelativeDescriptor:
    """What the LLM returns for a time phrase. Deliberately NOT a date.

    ADR-010 splits responsibility: the model identifies the phrase and its
    structure, and TimeResolver does the arithmetic. Models are reliable at
    spotting time expressions and unreliable at date maths, and their arithmetic
    errors are silent and unfalsifiable.
    """

    direction: TemporalDirection = TemporalDirection.NONE
    quantity: int | None = None
    unit: TemporalUnit | None = None
    weekday: int | None = None
    """0 = Monday ... 6 = Sunday, matching datetime.weekday()."""
    modifier: TemporalModifier | None = None

    def __post_init__(self) -> None:
        if self.weekday is not None and not 0 <= self.weekday <= 6:
            raise ValueError("weekday must be 0..6 (Monday..Sunday)")
        if self.quantity is not None and self.quantity < 0:
            raise ValueError("quantity must be non-negative; use direction for sign")

    @property
    def is_resolvable(self) -> bool:
        """Whether this descriptor carries enough information to resolve.

        Three resolvable shapes:
          - weekday + modifier      "last Tuesday"
          - quantity + unit + direction   "three weeks ago"
          - modifier + unit         "last month"
        """
        if self.weekday is not None and self.modifier is not None:
            return True
        if (
            self.quantity is not None
            and self.unit is not None
            and self.direction is not TemporalDirection.NONE
        ):
            return True
        if self.modifier is not None and self.unit is not None:
            return True
        return False


@dataclass(frozen=True, slots=True)
class TemporalExpression:
    """A time reference found in text, with its resolution.

    The raw phrase is never discarded (ADR-010). Keeping it allows re-resolution
    with a better resolver later and makes the resolution auditable, which is
    consistent with the provenance principle applied everywhere else.
    """

    raw_phrase: str
    granularity: Granularity
    method: ResolutionMethod
    anchor_zone: str
    """IANA zone active at capture (ADR-011). Stored per record so that history
    stays correct if the user relocates or travels."""
    descriptor: RelativeDescriptor | None = None
    resolved_from: datetime | None = None
    resolved_to: datetime | None = None
    """Half-open: [resolved_from, resolved_to)."""

    def __post_init__(self) -> None:
        _assert_aware(self.resolved_from, "resolved_from")
        _assert_aware(self.resolved_to, "resolved_to")

        # The UNKNOWN invariant. Without this, an unresolvable phrase could carry
        # a plausible-looking date and poison the timeline invisibly.
        if self.granularity is Granularity.UNKNOWN and (
            self.resolved_from is not None or self.resolved_to is not None
        ):
            raise ValueError("UNKNOWN granularity requires resolved dates to be None")

        if self.method is ResolutionMethod.UNRESOLVED and self.granularity is not Granularity.UNKNOWN:
            raise ValueError("UNRESOLVED method requires UNKNOWN granularity")

        if self.resolved_from and self.resolved_to and self.resolved_to < self.resolved_from:
            raise ValueError("resolved_to must not precede resolved_from")

    @property
    def is_resolved(self) -> bool:
        return self.resolved_from is not None


@dataclass(frozen=True, slots=True)
class OrderingConstraint:
    """A relative ordering that could not be reduced to a date.

    "Before the wedding" carries real temporal information even when no date can
    be established. ADR-010 stores it as an ordering relation rather than
    inventing a timestamp, because a fabricated date is worse than an honest
    partial ordering.
    """

    raw_phrase: str
    direction: RelationDirection
    reference_phrase: str
    """The event referred to, e.g. "the wedding". Resolved to an event id later
    if a match is found."""
