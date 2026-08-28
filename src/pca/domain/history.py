"""Belief history and the operation log.

Layer L0. Standard library only.

These two types carry the difference between a system that *has* memory and one that
can *account for* its memory.

`BeliefTransition` records what was believed and when, so that a belief which has
since been corrected away remains recoverable. Without it, `facts` holds only the
current belief and "what did I think was true in March?" becomes unanswerable the
moment a correction lands.

`MemoryOperation` records who changed what and why. Specification §12 requires
auditability of memory changes; ADR-014 requires entity merges to be reversible, and
a merge can only be reversed if it was recorded.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pca.domain.enums import BeliefChangeCause, MemoryKind, OperationKind
from pca.domain.ids import (
    EntityId,
    EpisodeId,
    MemoryId,
    OperationId,
)
from pca.domain.temporal import BeliefWindow, TemporalValidity


@dataclass(frozen=True, slots=True)
class BeliefTransition:
    """One belief the system has held about one memory.

    The statement and validity are **snapshotted** rather than referenced. A
    correction rewrites the live fact, so a reference would resolve to the corrected
    text and the earlier belief would be silently unrecoverable — which is precisely
    the failure this type exists to prevent.
    """

    id: MemoryId
    memory_id: MemoryId
    memory_kind: MemoryKind
    cause: BeliefChangeCause
    belief: BeliefWindow
    statement: str
    recorded_at: datetime
    validity: TemporalValidity = field(default_factory=TemporalValidity)
    superseded_by: MemoryId | None = None
    reason: str | None = None

    def held_at(self, when: datetime) -> bool:
        """Whether this belief was held at `when`.

        Reads the belief axis, never the world axis. Compare with
        `TemporalValidity.covers`, which answers the other question entirely.
        """
        return self.belief.held_at(when)


@dataclass(frozen=True, slots=True)
class MemoryOperation:
    """An entry in the append-only audit log."""

    id: OperationId
    operation: OperationKind
    performed_at: datetime
    memory_id: MemoryId | None = None
    memory_kind: MemoryKind | None = None
    entity_id: EntityId | None = None
    episode_id: EpisodeId | None = None
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CorrectionOutcome:
    """Result of correcting a mistaken memory.

    `original_id` keeps its world-time validity untouched — the fact was never true,
    so there is no period to preserve — but its belief window closes.
    """

    original_id: MemoryId
    replacement_id: MemoryId
    reason: str


@dataclass(frozen=True, slots=True)
class SupersessionOutcome:
    """Result of superseding a memory because the world changed.

    Deliberately different from a correction: the original keeps its belief (we still
    think it was true then) and gains a world-time `valid_to`. FR-04.4 requires the
    earlier state to survive.
    """

    original_id: MemoryId
    replacement_id: MemoryId
    effective_from: datetime


@dataclass(frozen=True, slots=True)
class TimelineDiff:
    """What changed between two instants (FR-04.6)."""

    start: datetime
    end: datetime
    became_true: list[str] = field(default_factory=list)
    ceased_to_be_true: list[str] = field(default_factory=list)
    corrected: list[str] = field(default_factory=list)
    """Statements the system stopped believing because it had been wrong — as opposed
    to statements that stopped being true. Keeping these separate is the point of the
    two axes."""

    @property
    def is_empty(self) -> bool:
        return not (self.became_true or self.ceased_to_be_true or self.corrected)
