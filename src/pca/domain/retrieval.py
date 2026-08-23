"""Retrieval and context-construction types.

Layer L0. Standard library only.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

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


@dataclass(frozen=True, slots=True)
class StrategySpend:
    """What one retrieval strategy cost and returned."""

    strategy: str
    duration: timedelta
    hits: int


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

    @property
    def total_duration(self) -> timedelta:
        return sum((s.duration for s in self.spends), timedelta())


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
