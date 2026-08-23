"""ConversationService — owns the immutable source material.

Layer L3. Depends on repository ports and ClockPort only. No SQL here.

FR-01.4 requires conversations to be preserved as source material that is never
replaced by an AI-generated summary. That is enforced by omission: this class
exposes no method to update or delete a message. The only mutation is append.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

from pca.domain.conversation import Conversation, Message
from pca.domain.enums import Role
from pca.domain.ids import ConversationId, MessageId
from pca.observability.logging import get_logger
from pca.ports.clock import ClockPort
from pca.ports.repositories import ConversationRepositoryPort

_log = get_logger(__name__)

DEFAULT_HISTORY_LIMIT = 50


class ConversationService:
    def __init__(
        self,
        repository: ConversationRepositoryPort,
        clock: ClockPort,
    ) -> None:
        self._repository = repository
        self._clock = clock

    async def create_conversation(self, title: str | None = None) -> Conversation:
        conversation = await self._repository.create_conversation(
            conversation_id=ConversationId(uuid4()),
            started_at=self._clock.now(),
            zone=self._clock.zone(),
            title=title,
        )
        _log.info(
            "conversation_created",
            conversation_id=str(conversation.id),
            zone=conversation.zone,
        )
        return conversation

    async def append_message(
        self,
        conversation_id: ConversationId,
        role: Role,
        content: str,
    ) -> Message:
        """Append a message and return it.

        The returned message's `captured_at` is the anchor against which every
        relative time reference in this text will later be resolved (ADR-010),
        and `zone` is the local zone that resolution must use (ADR-011). Both are
        captured here rather than at extraction time, because extraction may run
        minutes later in the background (ADR-008) and resolving "last Tuesday"
        against the extraction time instead of the utterance time would shift
        dates.
        """
        message = await self._repository.append_message(
            message_id=MessageId(uuid4()),
            conversation_id=conversation_id,
            role=role,
            content=content,
            captured_at=self._clock.now(),
            zone=self._clock.zone(),
        )
        _log.info(
            "message_appended",
            conversation_id=str(conversation_id),
            message_id=str(message.id),
            role=role.value,
            chars=len(content),
        )
        return message

    async def get_conversation(self, conversation_id: ConversationId) -> Conversation | None:
        return await self._repository.get_conversation(conversation_id)

    async def get_history(
        self,
        conversation_id: ConversationId,
        limit: int | None = DEFAULT_HISTORY_LIMIT,
    ) -> Sequence[Message]:
        """Recent messages in chronological order.

        A limit is applied by default. Long-conversation compaction is explicitly
        deferred (constraint C-13), so this is a bound rather than a strategy —
        it stops a 300-message conversation from silently filling the prompt while
        the real policy is still undecided.
        """
        return await self._repository.get_history(conversation_id, limit)

    async def list_conversations(
        self, limit: int = 20, offset: int = 0
    ) -> Sequence[Conversation]:
        return await self._repository.list_conversations(limit=limit, offset=offset)

    async def get_message(self, message_id: MessageId) -> Message | None:
        return await self._repository.get_message(message_id)

    async def get_surrounding(self, message_id: MessageId, window: int = 2) -> Sequence[Message]:
        return await self._repository.get_surrounding(message_id, window)
