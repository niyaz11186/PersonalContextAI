"""PostgreSQL implementation of ConversationRepositoryPort.

Layer L5. SQLAlchemy Core statements live here, never in L3 (boundary rule 3).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import and_, func, insert, or_, select

from pca.adapters.postgres.tables import conversations, messages
from pca.domain.conversation import Conversation, Message
from pca.domain.enums import Role
from pca.domain.ids import ConversationId, MessageId
from pca.ports.store import RelationalStorePort


def _to_conversation(row) -> Conversation:  # type: ignore[no-untyped-def]
    return Conversation(
        id=ConversationId(row["id"]),
        started_at=row["started_at"],
        zone=row["zone"],
        title=row["title"],
    )


def _to_message(row) -> Message:  # type: ignore[no-untyped-def]
    return Message(
        id=MessageId(row["id"]),
        conversation_id=ConversationId(row["conversation_id"]),
        role=Role(row["role"]),
        content=row["content"],
        captured_at=row["captured_at"],
        zone=row["zone"],
    )


class PostgresConversationRepository:
    def __init__(self, store: RelationalStorePort) -> None:
        self._store = store

    async def create_conversation(
        self,
        conversation_id: ConversationId,
        started_at: datetime,
        zone: str,
        title: str | None,
    ) -> Conversation:
        await self._store.execute(
            insert(conversations).values(
                id=conversation_id,
                title=title,
                started_at=started_at,
                zone=zone,
            )
        )
        return Conversation(id=conversation_id, started_at=started_at, zone=zone, title=title)

    async def get_conversation(self, conversation_id: ConversationId) -> Conversation | None:
        row = await self._store.fetch_one(
            select(conversations).where(
                and_(conversations.c.id == conversation_id, conversations.c.deleted_at.is_(None))
            )
        )
        return _to_conversation(row) if row else None

    async def list_conversations(self, limit: int, offset: int) -> Sequence[Conversation]:
        rows = await self._store.fetch_all(
            select(conversations)
            .where(conversations.c.deleted_at.is_(None))
            .order_by(conversations.c.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_to_conversation(row) for row in rows]

    async def append_message(
        self,
        message_id: MessageId,
        conversation_id: ConversationId,
        role: Role,
        content: str,
        captured_at: datetime,
        zone: str,
    ) -> Message:
        # seq is assigned inside the same statement rather than read-then-write,
        # so concurrent appends to one conversation cannot collide on a value.
        next_seq = (
            select(func.coalesce(func.max(messages.c.seq), 0) + 1)
            .where(messages.c.conversation_id == conversation_id)
            .scalar_subquery()
        )
        await self._store.execute(
            insert(messages).values(
                id=message_id,
                conversation_id=conversation_id,
                role=role.value,
                content=content,
                captured_at=captured_at,
                zone=zone,
                seq=next_seq,
            )
        )
        return Message(
            id=message_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            captured_at=captured_at,
            zone=zone,
        )

    async def get_message(self, message_id: MessageId) -> Message | None:
        row = await self._store.fetch_one(
            select(messages).where(messages.c.id == message_id)
        )
        return _to_message(row) if row else None

    async def get_history(
        self, conversation_id: ConversationId, limit: int | None = None
    ) -> Sequence[Message]:
        # Ordered by seq, not captured_at: two messages can share an instant and
        # timestamp ordering would be non-deterministic.
        statement = (
            select(messages)
            .where(messages.c.conversation_id == conversation_id)
            .order_by(messages.c.seq)
        )
        if limit is not None:
            # Take the LAST n by ordering desc, then restore chronological order.
            statement = (
                select(messages)
                .where(messages.c.conversation_id == conversation_id)
                .order_by(messages.c.seq.desc())
                .limit(limit)
            )
            rows = await self._store.fetch_all(statement)
            return [_to_message(row) for row in reversed(list(rows))]

        rows = await self._store.fetch_all(statement)
        return [_to_message(row) for row in rows]

    async def get_surrounding(self, message_id: MessageId, window: int) -> Sequence[Message]:
        """Messages either side of a target, for provenance excerpts (FR-09.3).

        Returns context rather than an isolated sentence, because a fact shown
        without its surroundings is hard for the user to judge.
        """
        target = await self._store.fetch_one(
            select(messages.c.conversation_id, messages.c.seq).where(
                messages.c.id == message_id
            )
        )
        if not target:
            return []

        rows = await self._store.fetch_all(
            select(messages)
            .where(
                and_(
                    messages.c.conversation_id == target["conversation_id"],
                    messages.c.seq >= target["seq"] - window,
                    messages.c.seq <= target["seq"] + window,
                )
            )
            .order_by(messages.c.seq)
        )
        return [_to_message(row) for row in rows]
