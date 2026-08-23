"""GeminiProviderAdapter — implements LLMProviderPort via the Google GenAI SDK.

Layer L5.

Constraint C-2: Gemini only. Notably this is not merely permitted but is the
*only* fully OpenAI-free path through this stack — Graphiti's Anthropic and Groq
integrations still require an OpenAI key for embeddings and reranking, whereas
Gemini covers LLM, embedding, and cross-encoding roles itself.

NFR-06.1: retry with backoff, then surface a clear failure. There is no fallback
provider (constraint C-11), so exhausted retries raise and DegradationPolicy
decides what the user sees.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from typing import TypeVar

from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel

from pca.domain.errors import ProviderUnavailable
from pca.observability.logging import get_logger
from pca.ports.llm import Prompt, ProviderHealth

T = TypeVar("T", bound=BaseModel)

_log = get_logger(__name__)

_MAX_ATTEMPTS = 4
_BASE_DELAY_SECONDS = 0.6
_MAX_DELAY_SECONDS = 8.0

# Substrings that indicate a retry may succeed. Everything else fails fast —
# retrying a malformed request or an auth failure just wastes the latency budget.
_RETRYABLE_MARKERS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "deadline",
    "timeout",
    "unavailable",
    "resource_exhausted",
    "rate limit",
)


def _is_retryable(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _RETRYABLE_MARKERS)


class GeminiProviderAdapter:
    """Gemini-backed implementation of LLMProviderPort."""

    def __init__(
        self,
        api_key: str,
        default_model: str,
        small_model: str | None = None,
        client: genai.Client | None = None,
    ) -> None:
        self._default_model = default_model
        self._small_model = small_model or default_model
        # Injectable for tests; constructed here otherwise.
        self._client = client or genai.Client(api_key=api_key)

    # ------------------------------------------------------------------ public

    async def complete(self, prompt: Prompt, *, model: str | None = None) -> str:
        target = model or self._default_model
        config = self._config(prompt)

        async def call() -> str:
            response = await self._client.aio.models.generate_content(
                model=target,
                contents=self._contents(prompt),
                config=config,
            )
            return response.text or ""

        return await self._with_retry(call, target, "complete")

    async def stream(self, prompt: Prompt, *, model: str | None = None) -> AsyncIterator[str]:
        """Token stream for SSE delivery.

        Retry is deliberately not applied once streaming has begun: partial
        output has already reached the client, and replaying from the start would
        duplicate text mid-response.
        """
        target = model or self._default_model
        try:
            stream = await self._client.aio.models.generate_content_stream(
                model=target,
                contents=self._contents(prompt),
                config=self._config(prompt),
            )
            async for chunk in stream:
                if chunk.text:
                    yield chunk.text
        except Exception as exc:  # noqa: BLE001 - translated to a domain error
            _log.error("gemini_stream_failed", model=target, error=str(exc))
            raise ProviderUnavailable(f"Gemini stream failed: {exc}") from exc

    async def structured(
        self,
        prompt: Prompt,
        schema: type[T],
        *,
        model: str | None = None,
    ) -> T:
        """Schema-constrained output.

        The workhorse for extraction and classification. Note that this returns
        the *descriptor* for a time phrase, not a date — ADR-010 keeps date
        arithmetic in TimeResolver precisely because model arithmetic fails
        silently.
        """
        target = model or self._default_model
        config = self._config(prompt)
        config.response_mime_type = "application/json"
        config.response_schema = schema

        async def call() -> T:
            response = await self._client.aio.models.generate_content(
                model=target,
                contents=self._contents(prompt),
                config=config,
            )
            parsed = response.parsed
            if isinstance(parsed, schema):
                return parsed
            # Fall back to validating the raw text: the SDK does not always
            # populate .parsed depending on model and schema shape.
            return schema.model_validate_json(response.text or "{}")

        return await self._with_retry(call, target, "structured")

    async def health(self) -> ProviderHealth:
        try:
            text = await self.complete(
                Prompt(system="Reply with the single word: ok"),
                model=self._small_model,
            )
            return ProviderHealth(
                healthy=bool(text.strip()),
                model=self._small_model,
                detail=text.strip()[:64] or None,
            )
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return ProviderHealth(healthy=False, model=self._small_model, detail=str(exc)[:200])

    # --------------------------------------------------------------- internals

    @staticmethod
    def _contents(prompt: Prompt) -> list[genai_types.Content]:
        contents: list[genai_types.Content] = []
        for message in prompt.messages:
            role = "model" if message.role == "assistant" else "user"
            contents.append(
                genai_types.Content(
                    role=role,
                    parts=[genai_types.Part.from_text(text=message.content)],
                )
            )
        if not contents:
            # The SDK requires at least one content entry.
            contents.append(
                genai_types.Content(role="user", parts=[genai_types.Part.from_text(text="")])
            )
        return contents

    @staticmethod
    def _config(prompt: Prompt) -> genai_types.GenerateContentConfig:
        return genai_types.GenerateContentConfig(
            system_instruction=prompt.system,
            temperature=prompt.temperature,
            # We never pass tools, but the SDK enables automatic function calling
            # by default and emits a warning on every call. Disabling it removes
            # that overhead and the log noise.
            automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )

    @staticmethod
    async def _with_retry(call, model: str, operation: str):  # type: ignore[no-untyped-def]
        last: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return await call()
            except Exception as exc:  # noqa: BLE001 - classified below
                last = exc
                if not _is_retryable(exc) or attempt == _MAX_ATTEMPTS:
                    break
                # Exponential backoff with jitter, so concurrent retrieval
                # strategies do not resynchronise onto the same retry instant.
                delay = min(_BASE_DELAY_SECONDS * 2 ** (attempt - 1), _MAX_DELAY_SECONDS)
                delay += random.uniform(0, delay * 0.25)  # noqa: S311 - not cryptographic
                _log.warning(
                    "gemini_retry",
                    operation=operation,
                    model=model,
                    attempt=attempt,
                    delay_seconds=round(delay, 2),
                    error=str(exc)[:200],
                )
                await asyncio.sleep(delay)

        _log.error("gemini_failed", operation=operation, model=model, error=str(last)[:300])
        raise ProviderUnavailable(f"Gemini {operation} failed after {_MAX_ATTEMPTS} attempts: {last}")
