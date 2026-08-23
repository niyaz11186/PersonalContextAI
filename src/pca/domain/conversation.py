"""Conversations, messages, and episodes — the immutable source material.

Layer L0. Standard library only.

FR-01.4 requires conversations to be preserved as source material. These types
are frozen and the services that own them expose no update or delete operation
for messages; append is the only mutation.
"""

from dataclasses import dataclass, field
from datetime import datetime

from pca.domain.enums import Role
from pca.domain.ids import ConversationId, DocumentId, EpisodeId, MessageId


@dataclass(frozen=True, slots=True)
class Conversation:
    id: ConversationId
    started_at: datetime
    zone: str
    """IANA zone active when the conversation began (ADR-011)."""
    title: str | None = None


@dataclass(frozen=True, slots=True)
class Message:
    id: MessageId
    conversation_id: ConversationId
    role: Role
    content: str
    captured_at: datetime
    """UTC instant. Serves as the resolution anchor for every relative time
    reference in this message (ADR-010)."""
    zone: str
    """IANA zone active at capture. Needed because day-boundary arithmetic for
    phrases like "last Tuesday" must run in local time, not UTC."""


@dataclass(frozen=True, slots=True)
class Episode:
    """A unit of content submitted to the memory graph.

    Persisted in PostgreSQL before graph ingestion so that the graph can be
    rebuilt by replay (ADR-005). The payload is stored verbatim, which is what
    makes replay byte-faithful and re-extraction with a better model possible.
    """

    id: EpisodeId
    content: str
    occurred_at: datetime
    zone: str
    conversation_id: ConversationId | None = None
    message_id: MessageId | None = None
    document_id: DocumentId | None = None
    ingested_at: datetime | None = None
    """None until the graph has accepted it. Non-null is the replay watermark."""


@dataclass(frozen=True, slots=True)
class SourceExcerpt:
    """Surrounding messages for a provenance reference.

    Returns context rather than an isolated sentence, because a fact shown
    without its surroundings is hard to judge.
    """

    messages: list[Message] = field(default_factory=list)
    highlight_message_id: MessageId | None = None
