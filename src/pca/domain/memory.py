"""Memory records: facts, events, entities, relationships, provenance.

Layer L0. Standard library only.
"""

from dataclasses import dataclass, field
from datetime import datetime

from pca.domain.enums import (
    Confidence,
    ConflictKind,
    EntityType,
    Origin,
    ResolutionOutcome,
)
from pca.domain.ids import (
    ConversationId,
    DocumentId,
    EntityId,
    EpisodeId,
    MemoryId,
    MessageId,
)
from pca.domain.temporal import BeliefWindow, TemporalExpression, TemporalValidity


@dataclass(frozen=True, slots=True)
class ProvenanceRef:
    """A single pointer from derived memory back to its source (FR-02.5)."""

    episode_id: EpisodeId
    conversation_id: ConversationId | None = None
    message_id: MessageId | None = None
    document_id: DocumentId | None = None


@dataclass(frozen=True, slots=True)
class Fact:
    """Something asserted about the world.

    `provenance` is a LIST, not a single reference. This is required by the
    corroboration rule in ADR-012: a fact supported by three conversations must
    survive deletion of one of them, and it is only retracted when the last
    supporting source is gone. A single reference cannot express that.
    """

    id: MemoryId
    statement: str
    origin: Origin
    confidence: Confidence
    validity: TemporalValidity
    belief: BeliefWindow
    provenance: list[ProvenanceRef]
    salience: float = 0.0
    """0.0 to 1.0 (ADR-017). Used to weight retrieval ranking, never to filter.
    Aggressive extraction (FR-02.2) means the graph accumulates trivia; the
    failure mode is not forgetting but burying, and salience is the counterweight."""
    subject_entity_ids: list[EntityId] = field(default_factory=list)
    temporal_expression: TemporalExpression | None = None
    superseded_by: MemoryId | None = None
    supersedes: MemoryId | None = None
    """Set when this fact replaced an earlier world state (Unit 3's `supersede`).

    Read by context assembly to populate `ContextPackage.currently_believed`.
    Without it that bucket has no meaning distinct from `user_stated`, because a
    fact that never superseded anything is simply the current belief by default.
    Knowing a fact REPLACED something is materially different information for the
    model: "Priya lives in Bangalore, superseding an earlier record" invites a
    different answer than the bare statement."""
    corrected_from: MemoryId | None = None
    """Set when this fact corrected a mistaken earlier record."""

    def __post_init__(self) -> None:
        if not 0.0 <= self.salience <= 1.0:
            raise ValueError("salience must be within 0.0..1.0")
        if not self.provenance:
            raise ValueError("a Fact must carry at least one provenance reference")

    @property
    def is_active(self) -> bool:
        """Currently believed and not superseded."""
        return self.belief.retracted_at is None and self.superseded_by is None

    @property
    def has_history(self) -> bool:
        """Whether this fact replaced or corrected an earlier record."""
        return self.supersedes is not None or self.corrected_from is not None


@dataclass(frozen=True, slots=True)
class Event:
    """Something that happened at a point or over a period."""

    id: MemoryId
    description: str
    origin: Origin
    provenance: list[ProvenanceRef]
    occurred_at: datetime | None = None
    occurred_through: datetime | None = None
    participant_entity_ids: list[EntityId] = field(default_factory=list)
    temporal_expression: TemporalExpression | None = None
    salience: float = 0.0

    def __post_init__(self) -> None:
        if not self.provenance:
            raise ValueError("an Event must carry at least one provenance reference")
        if (
            self.occurred_at
            and self.occurred_through
            and self.occurred_through < self.occurred_at
        ):
            raise ValueError("occurred_through must not precede occurred_at")


@dataclass(frozen=True, slots=True)
class Entity:
    """A person, organization, place, or project (FR-03.1)."""

    id: EntityId
    name: str
    entity_type: EntityType
    aliases: list[str] = field(default_factory=list)
    is_provisional: bool = False
    """True when created from an ambiguous mention (ADR-014). Surfaced via
    list_provisional so duplicates can be merged deliberately rather than
    accumulating unseen."""


@dataclass(frozen=True, slots=True)
class Relationship:
    """A typed, temporally scoped link between two entities (FR-03.2).

    Carries its own id. Without one, provenance could not point at a relationship
    and source deletion could not apply the corroboration rule to it — the
    relationship would be the one memory kind that silently escaped ADR-012.
    """

    id: MemoryId
    from_entity_id: EntityId
    to_entity_id: EntityId
    relation_type: str
    origin: Origin
    provenance: list[ProvenanceRef]
    validity: TemporalValidity = field(default_factory=TemporalValidity)

    def __post_init__(self) -> None:
        if self.from_entity_id == self.to_entity_id:
            raise ValueError("a relationship must connect two distinct entities")


@dataclass(frozen=True, slots=True)
class EntityMatch:
    """A candidate match during entity resolution (ADR-014)."""

    entity: Entity
    score: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("score must be within 0.0..1.0")


@dataclass(frozen=True, slots=True)
class ResolutionDecision:
    """Outcome of resolving a mention to an entity.

    Note the absence of a "merged" outcome — see ResolutionOutcome docstring.
    """

    outcome: ResolutionOutcome
    entity: Entity
    considered: list[EntityMatch] = field(default_factory=list)
    needs_clarification: bool = False


@dataclass(frozen=True, slots=True)
class CommitReceipt:
    """What a commit actually wrote.

    Returned rather than discarded so the caller can log it and so tests can assert
    on it. A commit that silently wrote nothing was the shape of the Unit 1b defect;
    a receipt makes that observable.
    """

    episode_id: EpisodeId
    fact_ids: list[MemoryId] = field(default_factory=list)
    event_ids: list[MemoryId] = field(default_factory=list)
    relationship_ids: list[MemoryId] = field(default_factory=list)
    entity_ids: list[EntityId] = field(default_factory=list)
    provisional_entity_ids: list[EntityId] = field(default_factory=list)
    """Entities created from ambiguous mentions (ADR-014). Non-empty means something
    needs a human decision."""

    @property
    def total(self) -> int:
        return (
            len(self.fact_ids)
            + len(self.event_ids)
            + len(self.relationship_ids)
            + len(self.entity_ids)
        )

    @property
    def needs_clarification(self) -> bool:
        return bool(self.provisional_entity_ids)


@dataclass(frozen=True, slots=True)
class Conflict:
    """A detected tension between a candidate and existing memory.

    Carries a classification, not a resolution. FR-05.6 requires surfacing
    contradictions rather than silently choosing a version, so this type
    deliberately has no "winner" field.
    """

    kind: ConflictKind
    incoming_statement: str
    existing_memory_id: MemoryId
    explanation: str
