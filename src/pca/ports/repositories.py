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
from pca.domain.ids import (
    ConversationId,
    EntityId,
    EpisodeId,
    MemoryId,
    MessageId,
)
from pca.domain.memory import Entity, Event, Fact, ProvenanceRef, Relationship


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

    async def add_aliases(self, entity_id: EntityId, aliases: Sequence[str]) -> None: ...

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

    Unit 2 scope is insertion and reading. The evolution semantics — correction,
    supersession, retraction, belief transitions — are Unit 3.
    """

    async def insert_fact(self, fact: Fact, salience_category: str | None) -> Fact: ...

    async def insert_event(self, event: Event, salience_category: str | None) -> Event: ...

    async def insert_relationship(self, relationship: Relationship) -> Relationship: ...

    async def get_fact(self, memory_id: MemoryId) -> Fact | None: ...

    async def active_facts(self, limit: int = 100) -> Sequence[Fact]:
        """Currently believed, not superseded, highest salience first."""
        ...

    async def facts_for_entity(self, entity_id: EntityId, limit: int = 50) -> Sequence[Fact]: ...

    async def relationships_for_entity(
        self, entity_id: EntityId
    ) -> Sequence[Relationship]: ...

    async def count_facts(self) -> int: ...


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
