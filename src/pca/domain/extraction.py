"""Extraction candidates — what the model proposes, before anything is committed.

Layer L0. Standard library only.

The separation between "candidate" and "committed memory" is deliberate and
load-bearing. Extraction produces candidates; conflict detection runs against
existing memory; only then does anything get written. Collapsing these steps would
mean writing first and detecting contradictions afterwards, which is how a graph
ends up holding two unrelated facts that quietly disagree (specification §6).
"""

from dataclasses import dataclass, field

from pca.domain.enums import Confidence, EntityType, Origin, SalienceCategory
from pca.domain.ids import EpisodeId
from pca.domain.temporal import OrderingConstraint, TemporalExpression


@dataclass(frozen=True, slots=True)
class CandidateFact:
    """A proposed fact. Not yet memory.

    `origin` is captured at proposal time and is immutable thereafter (FR-02.7).
    A candidate the model inferred can never later be presented as something the
    user stated.
    """

    statement: str
    origin: Origin
    confidence: Confidence = Confidence.PROBABLE
    salience: float = 0.0
    salience_category: SalienceCategory | None = None
    subject_names: list[str] = field(default_factory=list)
    """Entity *names* rather than ids — resolution to ids happens later, and
    ADR-014 forbids resolving ambiguous mentions silently."""
    temporal_expression: TemporalExpression | None = None


@dataclass(frozen=True, slots=True)
class CandidateEvent:
    description: str
    origin: Origin
    participant_names: list[str] = field(default_factory=list)
    temporal_expression: TemporalExpression | None = None
    salience: float = 0.0
    salience_category: SalienceCategory | None = None


@dataclass(frozen=True, slots=True)
class CandidateEntity:
    name: str
    entity_type: EntityType
    aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CandidateRelationship:
    from_name: str
    to_name: str
    relation_type: str
    origin: Origin


@dataclass(frozen=True, slots=True)
class ExtractionCandidates:
    """Everything one episode yielded."""

    episode_id: EpisodeId
    facts: list[CandidateFact] = field(default_factory=list)
    events: list[CandidateEvent] = field(default_factory=list)
    entities: list[CandidateEntity] = field(default_factory=list)
    relationships: list[CandidateRelationship] = field(default_factory=list)
    ordering_constraints: list[OrderingConstraint] = field(default_factory=list)
    """Populated when a time reference was event-relative and could not be dated.
    Preserved as a partial ordering rather than discarded or guessed (ADR-010)."""

    @property
    def is_empty(self) -> bool:
        return not (self.facts or self.events or self.entities or self.relationships)

    @property
    def total(self) -> int:
        return (
            len(self.facts)
            + len(self.events)
            + len(self.entities)
            + len(self.relationships)
        )
