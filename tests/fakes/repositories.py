"""In-memory repository fakes.

These are what let Unit 1b's domain services be developed and tested while no
container runtime is available. They implement the repository ports with dicts,
preserving the ordering and immutability guarantees the real adapters provide.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pca.domain.conversation import Conversation, Episode, Message
from pca.domain.enums import Role
from pca.domain.ids import ConversationId, EpisodeId, MessageId


class FakeConversationRepository:
    """Dict-backed ConversationRepositoryPort.

    Mirrors the real adapter's ordering contract: messages carry a monotonic
    sequence and history is ordered by it, not by timestamp. A fake that ordered
    by timestamp would hide non-determinism that the real store would expose.
    """

    def __init__(self) -> None:
        self.conversations: dict[ConversationId, Conversation] = {}
        self.messages: dict[MessageId, Message] = {}
        self._seq: dict[MessageId, int] = {}
        self._next_seq: dict[ConversationId, int] = {}
        self.deleted: set[ConversationId] = set()

    async def create_conversation(
        self,
        conversation_id: ConversationId,
        started_at: datetime,
        zone: str,
        title: str | None,
    ) -> Conversation:
        conversation = Conversation(
            id=conversation_id, started_at=started_at, zone=zone, title=title
        )
        self.conversations[conversation_id] = conversation
        self._next_seq[conversation_id] = 1
        return conversation

    async def get_conversation(self, conversation_id: ConversationId) -> Conversation | None:
        if conversation_id in self.deleted:
            return None
        return self.conversations.get(conversation_id)

    async def list_conversations(self, limit: int, offset: int) -> Sequence[Conversation]:
        live = [c for c in self.conversations.values() if c.id not in self.deleted]
        live.sort(key=lambda c: c.started_at, reverse=True)
        return live[offset : offset + limit]

    async def append_message(
        self,
        message_id: MessageId,
        conversation_id: ConversationId,
        role: Role,
        content: str,
        captured_at: datetime,
        zone: str,
    ) -> Message:
        message = Message(
            id=message_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            captured_at=captured_at,
            zone=zone,
        )
        self.messages[message_id] = message
        seq = self._next_seq.get(conversation_id, 1)
        self._seq[message_id] = seq
        self._next_seq[conversation_id] = seq + 1
        return message

    async def get_message(self, message_id: MessageId) -> Message | None:
        return self.messages.get(message_id)

    async def get_history(
        self, conversation_id: ConversationId, limit: int | None = None
    ) -> Sequence[Message]:
        found = [m for m in self.messages.values() if m.conversation_id == conversation_id]
        found.sort(key=lambda m: self._seq[m.id])
        if limit is not None:
            found = found[-limit:]
        return found

    async def get_surrounding(self, message_id: MessageId, window: int) -> Sequence[Message]:
        target = self.messages.get(message_id)
        if not target:
            return []
        centre = self._seq[message_id]
        found = [
            m
            for m in self.messages.values()
            if m.conversation_id == target.conversation_id
            and abs(self._seq[m.id] - centre) <= window
        ]
        found.sort(key=lambda m: self._seq[m.id])
        return found


class FakeEpisodeRepository:
    """Dict-backed EpisodeRepositoryPort."""

    def __init__(self) -> None:
        self.episodes: dict[EpisodeId, Episode] = {}
        self.models: dict[EpisodeId, tuple[str, str]] = {}
        self._created_order: list[EpisodeId] = []

    async def save(self, episode: Episode, llm_model: str, embedding_model: str) -> Episode:
        self.episodes[episode.id] = episode
        self.models[episode.id] = (llm_model, embedding_model)
        self._created_order.append(episode.id)
        return episode

    async def get(self, episode_id: EpisodeId) -> Episode | None:
        return self.episodes.get(episode_id)

    async def mark_ingested(self, episode_id: EpisodeId, ingested_at: datetime) -> None:
        existing = self.episodes.get(episode_id)
        if existing is None or existing.ingested_at is not None:
            # Idempotent: never overwrite an existing watermark.
            return
        self.episodes[episode_id] = Episode(
            id=existing.id,
            content=existing.content,
            occurred_at=existing.occurred_at,
            zone=existing.zone,
            conversation_id=existing.conversation_id,
            message_id=existing.message_id,
            document_id=existing.document_id,
            ingested_at=ingested_at,
        )

    async def pending(self, limit: int) -> Sequence[Episode]:
        found = [
            self.episodes[eid]
            for eid in self._created_order
            if self.episodes[eid].ingested_at is None
        ]
        return found[:limit]

    async def replay_batch(self, after: datetime | None, limit: int) -> Sequence[Episode]:
        found = sorted(self.episodes.values(), key=lambda e: e.occurred_at)
        if after is not None:
            found = [e for e in found if e.occurred_at > after]
        return found[:limit]

    async def count(self) -> int:
        return len(self.episodes)
