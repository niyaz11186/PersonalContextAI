"""Fake LLM provider.

Exists so that every service above L4 is testable without network access or an
API key. Scripted responses make assertions deterministic.
"""

from collections.abc import AsyncIterator
from typing import Any, TypeVar

from pydantic import BaseModel

from pca.domain.errors import ProviderUnavailable
from pca.ports.llm import Prompt, ProviderHealth

T = TypeVar("T", bound=BaseModel)


class FakeLLMProvider:
    """Scripted implementation of LLMProviderPort."""

    def __init__(
        self,
        completions: list[str] | None = None,
        structured_results: list[BaseModel] | None = None,
        healthy: bool = True,
        fail_with: Exception | None = None,
    ) -> None:
        self._completions = list(completions or ["ok"])
        self._structured = list(structured_results or [])
        self._healthy = healthy
        self._fail_with = fail_with
        self.calls: list[tuple[str, Prompt, Any]] = []

    async def complete(self, prompt: Prompt, *, model: str | None = None) -> str:
        self.calls.append(("complete", prompt, model))
        if self._fail_with:
            raise self._fail_with
        return self._completions.pop(0) if len(self._completions) > 1 else self._completions[0]

    async def stream(self, prompt: Prompt, *, model: str | None = None) -> AsyncIterator[str]:
        self.calls.append(("stream", prompt, model))
        if self._fail_with:
            raise self._fail_with
        text = self._completions[0]
        for token in text.split(" "):
            yield token + " "

    async def structured(
        self, prompt: Prompt, schema: type[T], *, model: str | None = None
    ) -> T:
        self.calls.append(("structured", prompt, model))
        if self._fail_with:
            raise self._fail_with
        if not self._structured:
            raise ProviderUnavailable("FakeLLMProvider has no scripted structured result")
        result = self._structured.pop(0)
        if not isinstance(result, schema):
            raise TypeError(f"scripted result is {type(result).__name__}, expected {schema.__name__}")
        return result

    async def health(self) -> ProviderHealth:
        return ProviderHealth(healthy=self._healthy, model="fake", detail=None)
