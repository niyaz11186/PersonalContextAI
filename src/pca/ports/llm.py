"""LLMProviderPort — our own direct model calls.

Layer L4.

Scope note (ADR-007): this port governs only the application's own calls.
Graphiti configures its own Gemini clients internally, and routing those through
this port would mean fighting the framework. The two coexist deliberately.

No LiteLLM. With a single provider it would be a dependency solving a problem we
do not have; the thin port is the seam for provider independence, and an adapter
for a second provider can be added behind it later.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class PromptMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class Prompt:
    messages: Sequence[PromptMessage] = field(default_factory=tuple)
    system: str | None = None
    temperature: float = 0.2


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    healthy: bool
    model: str
    detail: str | None = None


class LLMProviderPort(Protocol):
    """Text generation, streaming, and structured output."""

    async def complete(self, prompt: Prompt, *, model: str | None = None) -> str: ...

    def stream(self, prompt: Prompt, *, model: str | None = None) -> AsyncIterator[str]:
        """Token stream for SSE delivery (FR-01.2, constraint C-9)."""
        ...

    async def structured(
        self,
        prompt: Prompt,
        schema: type[T],
        *,
        model: str | None = None,
    ) -> T:
        """Schema-constrained output.

        The workhorse for extraction and classification. Gemini's structured
        output support is why ADR-002 is low risk — Graphiti's own extraction
        depends on it too, and weaker models cause ingestion failures.
        """
        ...

    async def health(self) -> ProviderHealth: ...
