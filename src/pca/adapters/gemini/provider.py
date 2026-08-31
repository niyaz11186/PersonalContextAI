"""GeminiProviderAdapter — implements LLMProviderPort via the Google GenAI SDK.

Layer L5.

Constraint C-2: Gemini only. Notably this is not merely permitted but is the
*only* fully OpenAI-free path through this stack — Graphiti's Anthropic and Groq
integrations still require an OpenAI key for embeddings and reranking, whereas
Gemini covers LLM, embedding, and cross-encoding roles itself.

NFR-06.1: retry with backoff, then surface a clear failure. There is no fallback
provider (constraint C-11), so exhausted retries raise and DegradationPolicy
decides what the user sees.

RESILIENCY-10 (added in Unit 5): every call is bounded twice — by an explicit
timeout, and by a semaphore capping concurrent calls. `services.md` §Concurrency
Model specified the semaphore during Inception and it was never built; the omission
was invisible while every model call sat on the request path and was therefore
serialised by one user typing. Unit 5's background extraction removes that
accidental limit, and without a cap a burst of messages can spawn enough concurrent
calls to exhaust the Gemini rate limit — which then times out every conversation's
write barrier at once, turning one saturated dependency into a whole-system stall.
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
        max_concurrency: int = 4,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._default_model = default_model
        self._small_model = small_model or default_model
        # Injectable for tests; constructed here otherwise.
        self._client = client or genai.Client(api_key=api_key)
        self._max_concurrency = max_concurrency
        self._gate = asyncio.Semaphore(max_concurrency)
        self._timeout = timeout_seconds

    @property
    def in_flight(self) -> int:
        """Calls currently holding a slot. Read by tests and /health."""
        return self._max_concurrency - self._gate._value  # noqa: SLF001

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
            async with self._gate:
                # The timeout covers establishing the stream, not consuming it. A
                # long stream is legitimate; one that never produces a first chunk
                # is the unbounded wait RESILIENCY-10 forbids.
                stream = await asyncio.wait_for(
                    self._client.aio.models.generate_content_stream(
                        model=target,
                        contents=self._contents(prompt),
                        config=self._config(prompt),
                    ),
                    timeout=self._timeout,
                )
                async for chunk in stream:
                    if chunk.text:
                        yield chunk.text
        except TimeoutError as exc:
            _log.error("gemini_stream_timeout", model=target, seconds=self._timeout)
            raise ProviderUnavailable(
                f"Gemini stream did not start within {self._timeout}s"
            ) from exc
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

    async def _with_retry(self, call, model: str, operation: str):  # type: ignore[no-untyped-def]
        last: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                # Slot acquired per attempt and released before the backoff sleep.
                # Holding it across an 8 s sleep would starve other callers of a
                # bulkhead that exists precisely to keep one stalled dependency from
                # consuming the whole budget.
                async with self._gate:
                    return await asyncio.wait_for(call(), timeout=self._timeout)
            except TimeoutError:  # noqa: PERF203 - retry loop
                # Retryable: a timeout is the transient case by definition. Without
                # this branch an explicit timeout would be strictly worse than none,
                # failing calls that the existing backoff would have recovered.
                last = ProviderUnavailable(
                    f"Gemini {operation} exceeded {self._timeout}s"
                )
                _log.warning(
                    "gemini_timeout",
                    operation=operation,
                    model=model,
                    attempt=attempt,
                    seconds=self._timeout,
                )
                if attempt == _MAX_ATTEMPTS:
                    break
                await asyncio.sleep(self._backoff(attempt))
            except Exception as exc:  # noqa: BLE001 - classified below
                last = exc
                if not _is_retryable(exc) or attempt == _MAX_ATTEMPTS:
                    break
                delay = self._backoff(attempt)
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

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential with jitter, so concurrent callers do not resynchronise."""
        delay = min(_BASE_DELAY_SECONDS * 2 ** (attempt - 1), _MAX_DELAY_SECONDS)
        return delay + random.uniform(0, delay * 0.25)  # noqa: S311 - not cryptographic
