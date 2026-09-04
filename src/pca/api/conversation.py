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

    As of Unit 5 extraction is **off the request path** (ADR-008), which retires the
    NFR-02.3 exception carried since Unit 1b. The full ordering is now:

        1. persist the user message      <- durability point, before any model call
        2. await the conversation's write barrier
        3. classify intent, then stream the reply
        4. record the episode and hand it to the coordinator, which extracts in
           the background

    Step 2 is what keeps the core hypothesis true without making the user wait: the
    previous message's extraction must be visible before this one is answered, but
    *this* message's extraction is not waited on. On timeout the barrier proceeds with
    a disclosure rather than blocking indefinitely.
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
        notices: list[str] = []

        # The ADR-008 barrier. Waits for the PREVIOUS message's extraction in this
        # conversation so that a fact just stated is retrievable now — the core
        # product hypothesis — without waiting for the current message's own
        # extraction, which is what NFR-02.3 forbids.
        barrier = await container.coordinator.await_barrier(conversation)
        if barrier.timed_out and barrier.degradation is not None:
            # Proceed rather than block forever. The abandoned extraction stays
            # durable and `recover_pending` retries it, but the reply about to be
            # generated may not include the last thing the user said.
            notices.append(barrier.degradation.disclosure)

        # Findings from the PREVIOUS turn's extraction, which finished after that
        # reply had been sent. Contradictions (FR-05.6) and entity ambiguity
        # (ADR-014) must be surfaced, and the barrier above has just guaranteed the
        # extraction that found them is complete — so this is the first moment they
        # can be reported. One turn late is the cost of ADR-008; dropping them would
        # be a requirement traded away for latency.
        notices.extend(container.coordinator.take_notices(conversation))

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

        # Persist the episode, then hand it to the coordinator and return. The
        # extraction itself — extract, detect conflicts, commit, supersede — now runs
        # in ExtractionWorkflow off the request path, so none of it extends this
        # response. That is the NFR-02.3 exception finally retired.
        #
        # Conflict and clarification notices consequently cannot be reported on THIS
        # turn: they are discovered after the reply has been sent. They surface on the
        # next turn instead, which is the honest consequence of moving extraction into
        # the background rather than a regression. Unit 6's inspection API is where
        # they become directly visible.
        try:
            episode = await container.episodes.record_message(user_message)
            queued = await container.coordinator.submit(episode.id, conversation)
            if not queued:
                # Already claimed — a retried request for the same episode. Idempotent
                # by the `episode_id` primary key (C-35), so this is a no-op, not a
                # failure.
                _log.info("extraction_already_claimed", episode_id=str(episode.id))
        except SourceOfRecordUnavailable as exc:
            # The message itself is already durable; only the episode row failed.
            _log.error(
                "episode_persist_failed",
                error=str(exc)[:300],
                consequence="message saved; not queued for extraction",
            )
            notices.append(
                container.degradation.on_memory_write_failure(exc).disclosure
            )
        except Exception as exc:  # noqa: BLE001 - the message is already durable
            _log.error(
                "extraction_submit_failed",
                error=str(exc)[:300],
                consequence="message saved; memory not searchable until recovery",
            )
            notices.append(
                container.degradation.on_memory_write_failure(exc).disclosure
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
