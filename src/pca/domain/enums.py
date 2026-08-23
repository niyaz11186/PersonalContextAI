"""Domain enumerations.

Layer L0. Standard library only — no imports from any other pca package.
"""

from enum import StrEnum


class Role(StrEnum):
    """Author of a message."""

    USER = "user"
    ASSISTANT = "assistant"


class Origin(StrEnum):
    """Where a memory came from.

    FR-02.4 requires distinguishing user-stated facts from AI inference.
    FR-02.7 requires that an inference never silently becomes a user-stated fact,
    which is enforced structurally: this value is set once at creation and there is
    no operation anywhere in the system that promotes AI_INFERRED to USER_STATED.
    """

    USER_STATED = "user_stated"
    AI_INFERRED = "ai_inferred"
    IMPORTED = "imported"


class Confidence(StrEnum):
    """How sure the system is about a memory."""

    CERTAIN = "certain"
    PROBABLE = "probable"
    UNCERTAIN = "uncertain"


class EntityType(StrEnum):
    """Kinds of entity the knowledge graph tracks (FR-03.1).

    Mirrored as custom Graphiti entity types per ADR-015 so the graph's own
    extraction stays aligned with this domain model.
    """

    PERSON = "person"
    ORGANIZATION = "organization"
    PLACE = "place"
    PROJECT = "project"
    OTHER = "other"


class Granularity(StrEnum):
    """Precision of a resolved time reference (ADR-010).

    This exists to stop vague phrases acquiring fake precision. "Last summer" is
    not a timestamp. Recording DAY precision for a YEAR-precision phrase is the
    most direct route to a confidently wrong timeline.

    UNKNOWN carries a hard invariant: resolved dates MUST be None.
    """

    INSTANT = "instant"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"
    UNKNOWN = "unknown"


class ResolutionMethod(StrEnum):
    """How a time reference was resolved (ADR-010)."""

    ABSOLUTE = "absolute"
    CLOCK_RELATIVE = "clock_relative"
    EVENT_RELATIVE = "event_relative"
    UNRESOLVED = "unresolved"


class TemporalDirection(StrEnum):
    """Direction of a relative time reference."""

    PAST = "past"
    FUTURE = "future"
    NONE = "none"


class TemporalUnit(StrEnum):
    """Unit of a relative time offset."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class TemporalModifier(StrEnum):
    """Qualifier attached to a period reference, e.g. "last month"."""

    LAST = "last"
    THIS = "this"
    NEXT = "next"


class BeliefChangeCause(StrEnum):
    """Why the system's belief about a memory changed.

    Distinguishing CORRECTED from SUPERSEDED matters: correction means the system
    recorded something wrongly, supersession means the world changed. They have
    different effects on world-time validity even though both preserve history.
    """

    ASSERTED = "asserted"
    CORRECTED = "corrected"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    SOURCE_DELETED = "source_deleted"


class ConflictKind(StrEnum):
    """Classification of an incoming candidate against existing memory.

    Only CONTRADICTION is surfaced to the user. FR-05.6 forbids silently choosing
    a winner, so there is deliberately no "resolve" outcome here.
    """

    AGREEMENT = "agreement"
    REFINEMENT = "refinement"
    TEMPORAL_CHANGE = "temporal_change"
    CONTRADICTION = "contradiction"


class ResolutionOutcome(StrEnum):
    """Result of entity resolution during extraction (ADR-014).

    There is no MERGED outcome. Merging is never a side effect of extraction —
    a wrongly merged entity is invisible corruption that contaminates every future
    answer about both people, whereas a duplicate is a visible, correctable
    annoyance. Erring toward duplication is the safer failure direction.
    """

    LINKED = "linked"
    PROVISIONAL = "provisional"
    CREATED = "created"


class RelationDirection(StrEnum):
    """Ordering relation used when an event-relative reference cannot be dated."""

    BEFORE = "before"
    AFTER = "after"
