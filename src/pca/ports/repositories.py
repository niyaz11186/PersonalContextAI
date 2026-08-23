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
from pca.domain.enums import Role
from pca.domain.ids import ConversationId, EpisodeId, MessageId


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
