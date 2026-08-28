"""Repository ports — the seam between domain services and storage.

Layer L4.

These exist to satisfy boundary rule 3 ("no sqlalchemy in L3"). A service holding
only the generic RelationalStorePort would have to build SQL statements itself,
which would spread PostgreSQL assumptions through the domain layer and forfeit
the replaceability that rule 3 protects. See
`aidlc-docs/construction/unit-1b-skeleton-activation/design-refinement-repositories.md`.

Every signature here speaks domain types only. No SQLAlchemy, no asyncpg, no rows.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from pca.domain.conversation import Conversation, Episode, Message
from pca.domain.enums import EntityType, MemoryKind, Role
from pca.domain.history import BeliefTransition, MemoryOperation
from pca.domain.ids import (
    ConversationId,
    EntityId,
    EpisodeId,
    MemoryId,
    MessageId,
)
from pca.domain.memory import Entity, Event, Fact, ProvenanceRef, Relationship
from pca.ports.store import Transaction

# Write methods accept an optional `tx` so that a caller can compose several
# repository writes into one atomic unit (Unit 3). `Transaction` is this project's
# own protocol from ports.store, not a SQLAlchemy type, so boundary rule 3 holds:
# no storage library leaks into L3.
#
# The alternative considered was a UnitOfWork port bundling every repository. It was
# rejected as more machinery for the same guarantee — services would then depend on
# the bundle rather than on the one or two repositories they actually use.


class ConversationRepositoryPort(Protocol):
    """Conversations and messages — immutable source material (FR-01.4).

    Note the absence of update_message and delete_message. Append-only is
    enforced by the shape of this interface rather than by a database trigger,
    because the erase path (NFR-01.6) must still be able to purge.
    """

    async def create_conversation(
        self, conversation_id: ConversationId, started_at: datetime, zone: str, title: str | None
    ) -> Conversation: ...

    async def get_conversation(self, conversation_id: ConversationId) -> Conversation | None: ...

    async def list_conversations(self, limit: int, offset: int) -> Sequence[Conversation]: ...

    async def append_message(
        self,
        message_id: MessageId,
        conversation_id: ConversationId,
        role: Role,
        content: str,
        captured_at: datetime,
        zone: str,
    ) -> Message: ...

    async def get_message(self, message_id: MessageId) -> Message | None: ...

    async def get_history(
        self, conversation_id: ConversationId, limit: int | None = None
    ) -> Sequence[Message]:
        """Messages in conversation order.

        Ordered by the monotonic sequence column, not by timestamp: two messages
        can share an instant, and timestamp ordering would be non-deterministic.
        """
        ...

    async def get_surrounding(
        self, message_id: MessageId, window: int
    ) -> Sequence[Message]:
        """Messages either side of a target, for provenance excerpts (FR-09.3)."""
        ...


class EntityRepositoryPort(Protocol):
    """Entities and their aliases (FR-03.1, FR-03.4).

    Lookup is deliberately by name and alias rather than by embedding similarity.
    ADR-014 requires that ambiguity be *detected* rather than resolved, and a
    similarity score is exactly the kind of signal that invites silent merging.
    """

    async def create(
        self,
        entity_id: EntityId,
        name: str,
        entity_type: EntityType,
        created_at: datetime,
        is_provisional: bool = False,
        aliases: Sequence[str] = (),
        tx: Transaction | None = None,
    ) -> Entity: ...

    async def get(self, entity_id: EntityId) -> Entity | None: ...

    async def find_by_name(self, name: str) -> Sequence[Entity]:
        """Case-insensitive match on name or alias.

        Returns **all** matches, including duplicates. Returning a single "best"
        entity here would hide the ambiguity that ADR-014 exists to surface.
        """
        ...

    async def list_provisional(self, limit: int = 100) -> Sequence[Entity]:
        """Entities created from ambiguous mentions, for deliberate review."""
        ...

    async def add_aliases(
        self,
        entity_id: EntityId,
        aliases: Sequence[str],
        tx: Transaction | None = None,
    ) -> None: ...

    async def merge(
        self,
        keep: EntityId,
        absorb: EntityId,
        reason: str,
        merged_at: datetime,
    ) -> None:
        """Point one entity at another.

        Records rather than destroys: the absorbed row is retained with
        `merged_into` set, so the operation stays reversible. ADR-014 makes merging
        an explicit act precisely because a wrong merge is invisible corruption.
        """
        ...

    async def count(self) -> int: ...


class MemoryRepositoryPort(Protocol):
    """Facts, events, and relationships.

    Unit 3 adds the evolution semantics. Note that the two "ending" operations are
    separate methods rather than one `update` — `end_belief` and `end_validity` touch
    different time axes and mean genuinely different things. A single setter taking a
    column name would make it easy to write the wrong one, which is the exact mistake
    that silently corrupts a timeline.
    """

    async def insert_fact(
        self,
        fact: Fact,
        salience_category: str | None,
        tx: Transaction | None = None,
    ) -> Fact: ...

    async def insert_event(
        self,
        event: Event,
        salience_category: str | None,
        tx: Transaction | None = None,
    ) -> Event: ...

    async def insert_relationship(
        self, relationship: Relationship, tx: Transaction | None = None
    ) -> Relationship: ...

    async def get_fact(self, memory_id: MemoryId) -> Fact | None: ...

    async def active_facts(self, limit: int = 100) -> Sequence[Fact]:
        """Currently believed, not superseded, highest salience first."""
        ...

    async def facts_for_entity(self, entity_id: EntityId, limit: int = 50) -> Sequence[Fact]: ...

    async def relationships_for_entity(
        self, entity_id: EntityId
    ) -> Sequence[Relationship]: ...

    async def count_facts(self) -> int: ...

    # -------------------------------------------------------- evolution (Unit 3)

    async def end_belief(
        self,
        memory_id: MemoryId,
        retracted_at: datetime,
        tx: Transaction | None = None,
    ) -> None:
        """Stop believing a fact. Sets `retracted_at`, the belief axis.

        Used by correction and retraction. Explicitly NOT used by supersession: when
        the world changes we still believe the old fact was true for its window, and
        retracting it would erase the history FR-04.4 requires us to keep.
        """
        ...

    async def end_validity(
        self,
        memory_id: MemoryId,
        valid_to: datetime,
        tx: Transaction | None = None,
    ) -> None:
        """Record that a fact stopped being true in the world. Sets `valid_to`.

        Used by supersession. The belief window is left alone.
        """
        ...

    async def link_supersession(
        self,
        original_id: MemoryId,
        replacement_id: MemoryId,
        tx: Transaction | None = None,
    ) -> None:
        """Wire the pair in both directions: `superseded_by` and `supersedes`."""
        ...

    async def link_correction(
        self,
        original_id: MemoryId,
        replacement_id: MemoryId,
        tx: Transaction | None = None,
    ) -> None:
        """Mark the replacement as correcting the original (`corrected_from`)."""
        ...

    async def facts_valid_at(self, when: datetime, limit: int = 200) -> Sequence[Fact]:
        """Facts true in the world at `when`, per currently-held belief.

        The world axis. Excludes retracted facts, because a fact we no longer believe
        was never true at any time. Contrast `BeliefRepositoryPort.believed_at`.
        """
        ...

    async def facts_asserted_between(
        self, start: datetime, end: datetime
    ) -> Sequence[Fact]:
        """Facts first believed within a window. Feeds TimelineService.diff."""
        ...


class ProvenanceRepositoryPort(Protocol):
    """The many-to-many link from derived memory back to source episodes.

    Many-to-many is required by ADR-012's corroboration rule: a fact supported by
    three conversations must survive deletion of one, and only be retracted when the
    last supporting source is gone.
    """

    async def record(
        self,
        memory_id: MemoryId,
        memory_kind: MemoryKind,
        ref: ProvenanceRef,
        recorded_at: datetime,
        tx: Transaction | None = None,
    ) -> None: ...

    async def for_memory(
        self, memory_id: MemoryId, memory_kind: MemoryKind
    ) -> Sequence[ProvenanceRef]: ...

    async def count_for_memory(self, memory_id: MemoryId, memory_kind: MemoryKind) -> int:
        """Remaining supporting sources. The corroboration rule reads this."""
        ...

    async def memories_from_episode(
        self, episode_id: EpisodeId
    ) -> Sequence[tuple[MemoryId, MemoryKind]]: ...


class EpisodeRepositoryPort(Protocol):
    """Episodes — the graph replay source (ADR-005).

    The exact payload sent to the graph is persisted here so that a rebuild is
    byte-faithful and re-extraction with a better model stays possible. This is
    what makes Neo4j genuinely disposable.
    """

    async def save(self, episode: Episode, llm_model: str, embedding_model: str) -> Episode: ...

    async def get(self, episode_id: EpisodeId) -> Episode | None: ...

    async def mark_ingested(self, episode_id: EpisodeId, ingested_at: datetime) -> None:
        """Advance the replay watermark. Idempotent."""
        ...

    async def pending(self, limit: int) -> Sequence[Episode]:
        """Episodes not yet accepted by the graph.

        Used at startup to recover work lost to a crash, and by reindex to resume.
        """
        ...

    async def replay_batch(
        self, after: datetime | None, limit: int
    ) -> Sequence[Episode]:
        """Ordered episodes for rebuilding the graph from source."""
        ...

    async def count(self) -> int: ...


class BeliefRepositoryPort(Protocol):
    """Belief history — every belief the system has ever held (FR-04.8, FR-05.5).

    This exists because `facts` holds only the *current* belief. The moment a
    correction rewrites a statement, what the system previously thought becomes
    unrecoverable from `facts` alone. Answering "what did I think was true in March?"
    requires the belief held in March, which is what these rows preserve.

    Append-only by interface. There is no update or delete method, because a belief
    history that can be rewritten cannot support the audit questions it exists for.
    """

    async def record(
        self, transition: BeliefTransition, tx: Transaction | None = None
    ) -> BeliefTransition: ...

    async def close_open_transition(
        self,
        memory_id: MemoryId,
        memory_kind: MemoryKind,
        retracted_at: datetime,
        tx: Transaction | None = None,
    ) -> None:
        """Close the currently-open belief window for a memory.

        Called before opening a new one, so that belief windows for a single memory
        never overlap. Overlapping windows would make `believed_at` return two
        contradictory beliefs for the same instant.
        """
        ...

    async def for_memory(
        self, memory_id: MemoryId, memory_kind: MemoryKind
    ) -> Sequence[BeliefTransition]:
        """Full belief trail for one memory, oldest first."""
        ...

    async def believed_at(
        self, when: datetime, limit: int = 200
    ) -> Sequence[BeliefTransition]:
        """What the system believed at `when`.

        The belief axis. Returns the snapshotted statements as they stood, which is
        why this can differ from `facts_valid_at` for the same instant — and that
        difference is the observable proof that both axes are being tracked.
        """
        ...

    async def transitions_between(
        self, start: datetime, end: datetime, causes: Sequence[str] = ()
    ) -> Sequence[BeliefTransition]:
        """Belief windows that CLOSED within a window, optionally filtered by cause.

        Needed because a corrected fact cannot be found by comparing world-time state
        at two instants: `facts_valid_at` excludes retracted facts, so a correction
        removes the fact from BOTH endpoints and the comparison sees nothing. The only
        record that a correction happened during the window lives here.
        """
        ...

    async def count(self) -> int: ...


class OperationLogRepositoryPort(Protocol):
    """The append-only audit log of memory mutations (specification §12).

    Deliberately has no update or delete method. ADR-014 makes entity merges
    reversible, and reversal is only possible if the merge was recorded; a log that
    could be edited would make that guarantee unenforceable.
    """

    async def append(
        self, operation: MemoryOperation, tx: Transaction | None = None
    ) -> MemoryOperation: ...

    async def recent(self, limit: int = 50) -> Sequence[MemoryOperation]: ...

    async def for_memory(
        self, memory_id: MemoryId, limit: int = 50
    ) -> Sequence[MemoryOperation]: ...

    async def for_entity(
        self, entity_id: EntityId, limit: int = 50
    ) -> Sequence[MemoryOperation]: ...

    async def count(self) -> int: ...
