"""Conversation endpoints.

Layer L1.

Streaming uses Server-Sent Events (constraint C-9). SSE rather than WebSocket
because token delivery is one-directional: the client sends a message and receives
a stream. A WebSocket would add connection lifecycle management for no benefit here.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from pca.api.schemas import (
    ConversationResponse,
    CreateConversationRequest,
    MessageResponse,
    SendMessageRequest,
)
from pca.composition import Container
from pca.domain.enums import Role
from pca.domain.errors import ProviderUnavailable, SourceOfRecordUnavailable
from pca.domain.ids import ConversationId
from pca.observability.logging import get_logger, new_correlation_id

_log = get_logger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _container(request: Request) -> Container:
    return request.app.state.container


@router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: CreateConversationRequest, request: Request
) -> ConversationResponse:
    container = _container(request)
    try:
        conversation = await container.conversations.create_conversation(title=payload.title)
    except SourceOfRecordUnavailable as exc:
        # Constraint C-22: no degradation for the system of record. Accepting work
        # we cannot durably store would break the product's core promise.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    return ConversationResponse(
        id=conversation.id,
        title=conversation.title,
        started_at=conversation.started_at,
        zone=conversation.zone,
    )


@router.get("", response_model=list[ConversationResponse])
async def list_conversations(
    request: Request, limit: int = 20, offset: int = 0
) -> list[ConversationResponse]:
    container = _container(request)
    conversations = await container.conversations.list_conversations(
        limit=min(limit, 100), offset=offset
    )
    return [
        ConversationResponse(
            id=c.id, title=c.title, started_at=c.started_at, zone=c.zone
        )
        for c in conversations
    ]


@router.get("/{conversation_id}/messages", response_model=list[MessageResponse])
async def get_messages(
    conversation_id: UUID, request: Request, limit: int | None = None
) -> list[MessageResponse]:
    container = _container(request)
    history = await container.conversations.get_history(
        ConversationId(conversation_id), limit
    )
    return [
        MessageResponse(
            id=m.id,
            conversation_id=m.conversation_id,
            role=m.role.value,
            content=m.content,
            captured_at=m.captured_at,
            zone=m.zone,
        )
        for m in history
    ]


@router.post("/{conversation_id}/messages")
async def send_message(
    conversation_id: UUID, payload: SendMessageRequest, request: Request
) -> StreamingResponse:
    """Append a message and stream the reply.

    Ordering is deliberate and matches the ADR-005 write path:

        1. persist the user message      <- durability point, before any model call
        2. stream the reply
        3. record and ingest the episode

    Step 1 first means that if Gemini is unreachable the user's words are still
    recorded. Step 3 last means extraction never blocks the reply.

    Extraction runs **synchronously after** the stream in this unit, which knowingly
    violates NFR-02.3. Unit 5's ExtractionCoordinator introduces the durable
    per-conversation barrier that retires the exception (ADR-008).
    """
    container = _container(request)
    correlation = new_correlation_id()
    conversation = ConversationId(conversation_id)

    existing = await container.conversations.get_conversation(conversation)
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")

    try:
        user_message = await container.conversations.append_message(
            conversation, Role.USER, payload.content
        )
    except SourceOfRecordUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    async def event_stream() -> AsyncIterator[str]:
        reply: list[str] = []
        try:
            async for token in container.conversation_workflow.run(
                conversation, payload.content
            ):
                reply.append(token)
                yield f"data: {json.dumps({'token': token})}\n\n"
        except ProviderUnavailable as exc:
            # NFR-06.5: disclose rather than fail silently. The user must not be
            # left with a truncated reply that looks complete.
            _log.error("generation_failed", error=str(exc)[:200])
            yield f"data: {json.dumps({'error': 'The model is unavailable. Your message was saved.'})}\n\n"

        text = "".join(reply)
        if text.strip():
            await container.conversations.append_message(
                conversation, Role.ASSISTANT, text
            )

        # Episode recording and extraction happen after the reply so they never
        # delay the visible response.
        #
        # They still run before the terminal `done` event, which means a slow
        # extraction extends the request. That is the knowing NFR-02.3 exception
        # carried since Unit 1b: ADR-008's ExtractionCoordinator in Unit 5 moves
        # this off the request entirely behind a durable per-conversation barrier.
        notices: list[str] = []
        try:
            episode = await container.episodes.record_and_ingest(user_message)
            candidates = await container.extraction.extract(episode)
            receipt = await container.memory.commit(candidates, episode)

            if receipt.needs_clarification:
                # ADR-014: an ambiguous entity means this memory may be attached to
                # the wrong person until someone decides. Surfaced rather than
                # buried in a log, because silent ambiguity is how a graph quietly
                # becomes wrong.
                notices.append(
                    "One or more people mentioned could not be identified "
                    "unambiguously. The details were saved separately pending review."
                )
        except Exception as exc:  # noqa: BLE001 - the message is already durable
            _log.error(
                "post_reply_memory_write_failed",
                error=str(exc)[:300],
                consequence="message saved; memory not searchable until recovery",
            )
            notices.append(
                "Your message was saved, but it could not be added to memory just now."
            )

        # Deliberately not named `payload`: assigning that name anywhere in this
        # generator would shadow the request body captured from the enclosing scope
        # and turn the earlier `payload.content` read into an UnboundLocalError.
        done_event: dict[str, object] = {"done": True, "correlation_id": correlation}
        if notices:
            done_event["notices"] = notices
        yield f"data: {json.dumps(done_event)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # stops proxies buffering the stream
            "X-Correlation-Id": correlation,
        },
    )
