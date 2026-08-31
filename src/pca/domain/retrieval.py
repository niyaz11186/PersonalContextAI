"""Retrieval and context-construction types.

Layer L0. Standard library only.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from pca.domain.conversation import Message, SourceExcerpt
from pca.domain.ids import EntityId
from pca.domain.memory import Conflict, Entity, Event, Fact, Relationship


@dataclass(frozen=True, slots=True)
class RetrievalBudget:
    """Bounds on a retrieval attempt.

    FR-06.3 asks for the smallest useful set rather than everything similar,
    which requires an explicit stop condition. A fixed `limit` is not a stop
    condition; it is a truncation.
    """

    max_duration: timedelta
    max_items: int
    max_context_chars: int


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    text: str
    budget: RetrievalBudget
    time_range: tuple[datetime | None, datetime | None] | None = None
    entity_scope: list[EntityId] | None = None


class RetrievalStrategy(StrEnum):
    """The five strategies fused by RetrievalService (FR-06.2).

    Named rather than free-text so diagnostics can be aggregated across requests.
    A typo in a string label would silently split one strategy's statistics into
    two, which is the kind of defect that makes latency tuning quietly wrong.

    TRAVERSAL is deliberately not run concurrently with the others: it is seeded
    from the fused result set, and traversing from a bad seed is how irrelevant
    context floods the package (FR-06.5).
    """

    SEMANTIC = "semantic"
    FULLTEXT = "fulltext"
    ENTITY = "entity"
    TEMPORAL = "temporal"
    TRAVERSAL = "traversal"


@dataclass(frozen=True, slots=True)
class StrategySpend:
    """What one retrieval strategy cost and returned.

    `hits` is the count this strategy returned BEFORE fusion and dedup, which is
    what makes per-strategy contribution measurable. Recording the post-fusion
    number would attribute the same hit to every strategy that found it.
    """

    strategy: str
    duration: timedelta
    hits: int
    failed: bool = False
    """True when the strategy raised. Distinguished from `hits == 0`: finding
    nothing and being unable to look are different facts about the system."""


@dataclass(frozen=True, slots=True)
class Spend:
    """What retrieval has consumed so far. Input to the stop condition."""

    elapsed: timedelta
    chars: int


@dataclass(frozen=True, slots=True)
class RetrievalDiagnostics:
    """What ran, what it cost, what was discarded.

    Always travels with results. A 25-second latency budget cannot be tuned
    blind, and NFR-05.6 requires observability. This is also the third
    evaluation seam in ADR-016: persisting these allows offline scoring of
    retrieval quality without re-running conversations.
    """

    spends: list[StrategySpend] = field(default_factory=list)
    fused_count: int = 0
    reranked_count: int = 0
    dropped_by_budget: int = 0
    degraded: bool = False
    notes: list[str] = field(default_factory=list)
    stopped_early: bool = False
    """True when the governor halted retrieval before every strategy was
    exhausted. Separate from `dropped_by_budget`, which counts trimmed results —
    stopping early and trimming afterwards are different behaviours and
    conflating them would hide which one the budget actually triggered."""
    stop_reason: str | None = None

    @property
    def total_duration(self) -> timedelta:
        return sum((s.duration for s in self.spends), timedelta())

    @property
    def contributing_strategies(self) -> list[str]:
        """Strategies that returned at least one hit.

        The completion criterion for Unit 4 requires showing which strategies
        contributed, which is not the same as which ones ran.
        """
        return [s.strategy for s in self.spends if s.hits > 0 and not s.failed]

    @property
    def failed_strategies(self) -> list[str]:
        return [s.strategy for s in self.spends if s.failed]


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    facts: list[Fact] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    source_messages: list[Message] = field(default_factory=list)
    diagnostics: RetrievalDiagnostics = field(default_factory=RetrievalDiagnostics)


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    when: datetime | None
    description: str
    is_uncertain: bool = False


@dataclass(frozen=True, slots=True)
class Timeline:
    entries: list[TimelineEntry] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ContextPackage:
    """The explicit context handed to the model (FR-07.1).

    The four-way distinction required by FR-07.2 is **structural** rather than a
    label on a flat list. Separate fields make it impossible to render the
    package while accidentally collapsing "what you told me" into "what I
    inferred", which is the mechanism behind hallucinated history.
    """

    user_stated: list[Fact] = field(default_factory=list)
    system_derived: list[Fact] = field(default_factory=list)
    currently_believed: list[Fact] = field(default_factory=list)
    uncertain: list[Fact] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    source_excerpts: list[SourceExcerpt] = field(default_factory=list)
    conversation_history: list[Message] = field(default_factory=list)
    timeline: Timeline | None = None
    degradation_notices: list[str] = field(default_factory=list)
    """Populated when NFR-06.5 applies. Non-empty means the answer must disclose
    that context may be incomplete."""

    @property
    def is_empty(self) -> bool:
        return not (
            self.user_stated
            or self.system_derived
            or self.currently_believed
            or self.uncertain
            or self.events
            or self.conversation_history
        )
