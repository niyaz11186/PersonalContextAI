"""API request and response schemas.

Layer L1. HTTP-facing shapes only — these never leak below the API layer.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class CreateConversationRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class ConversationResponse(BaseModel):
    id: UUID
    title: str | None
    started_at: datetime
    zone: str


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)


class MessageResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    role: str
    content: str
    captured_at: datetime
    zone: str


class DependencyHealth(BaseModel):
    name: str
    healthy: bool
    detail: str | None = None


class HealthResponse(BaseModel):
    healthy: bool
    dependencies: list[DependencyHealth]
    note: str | None = None
