"""Verify GeminiProviderAdapter against the live API and choose model pins.

ADR-002 requires model identifiers to be verified, not assumed. This script does
that verification and, critically, tests the capability the system actually
depends on: **structured output**.

Structured output matters more than raw generation quality here because both our
extraction pipeline and Graphiti's internal entity extraction rely on it. A model
that generates fluent prose but returns malformed schemas is useless for this
application and will fail at ingestion time rather than obviously.

The structured test uses a RelativeDescriptor-shaped schema on purpose — that is
the real ADR-010 use case, where the model identifies a time phrase and returns
its *structure* while TimeResolver does the arithmetic.

Run:  .\\venv\\Scripts\\python.exe scripts\\verify_gemini.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

sys.path.insert(0, "src")

from pca.adapters.gemini.provider import GeminiProviderAdapter  # noqa: E402
from pca.ports.llm import Prompt, PromptMessage  # noqa: E402

LLM_CANDIDATES = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
]
SMALL_CANDIDATES = ["gemini-3.5-flash-lite", "gemini-2.5-flash-lite"]
EMBED_CANDIDATES = ["gemini-embedding-001", "gemini-embedding-2"]

SAMPLE = "I had a big argument with my sister Priya last Tuesday about the house in Pune."


class TimePhrase(BaseModel):
    """Mirrors RelativeDescriptor. Note it returns NO date — by design (ADR-010)."""

    raw_phrase: str = Field(description="the exact time phrase found in the text")
    direction: Literal["past", "future", "none"]
    quantity: int | None = Field(default=None)
    unit: Literal["day", "week", "month", "quarter", "year"] | None = Field(default=None)
    weekday: int | None = Field(default=None, description="0=Monday..6=Sunday, null if absent")
    modifier: Literal["last", "this", "next"] | None = Field(default=None)


class Extraction(BaseModel):
    people: list[str]
    places: list[str]
    time_phrases: list[TimePhrase]


async def try_llm(api_key: str, model: str) -> dict[str, object]:
    adapter = GeminiProviderAdapter(api_key=api_key, default_model=model)
    result: dict[str, object] = {"model": model}

    # 1. plain completion
    try:
        started = time.perf_counter()
        text = await adapter.complete(
            Prompt(
                system="Answer in exactly one word.",
                messages=[PromptMessage(role="user", content="What is 2+2?")],
            )
        )
        result["complete"] = f"OK ({text.strip()[:20]!r})"
        result["complete_ms"] = round((time.perf_counter() - started) * 1000)
    except Exception as exc:
        result["complete"] = f"FAIL {type(exc).__name__}: {str(exc)[:90]}"
        return result

    # 2. streaming
    try:
        chunks: list[str] = []
        async for chunk in adapter.stream(
            Prompt(messages=[PromptMessage(role="user", content="Count 1 to 5.")])
        ):
            chunks.append(chunk)
        result["stream"] = f"OK ({len(chunks)} chunks)"
    except Exception as exc:
        result["stream"] = f"FAIL {type(exc).__name__}: {str(exc)[:90]}"

    # 3. structured output — the decisive test
    try:
        started = time.perf_counter()
        extraction = await adapter.structured(
            Prompt(
                system=(
                    "Extract people, places, and time phrases. For each time phrase "
                    "return its STRUCTURE only. Never compute or return a date."
                ),
                messages=[PromptMessage(role="user", content=SAMPLE)],
            ),
            Extraction,
        )
        phrase = extraction.time_phrases[0] if extraction.time_phrases else None
        result["structured"] = "OK"
        result["structured_ms"] = round((time.perf_counter() - started) * 1000)
        result["people"] = extraction.people
        result["places"] = extraction.places
        result["phrase"] = (
            f"{phrase.raw_phrase!r} weekday={phrase.weekday} modifier={phrase.modifier}"
            if phrase
            else "NONE FOUND"
        )
    except Exception as exc:
        result["structured"] = f"FAIL {type(exc).__name__}: {str(exc)[:90]}"

    return result


async def try_embedding(api_key: str, model: str) -> str:
    from google import genai

    client = genai.Client(api_key=api_key)
    try:
        response = await client.aio.models.embed_content(model=model, contents=SAMPLE)
        values = response.embeddings[0].values or []
        return f"OK (dimension {len(values)})"
    except Exception as exc:
        return f"FAIL {type(exc).__name__}: {str(exc)[:90]}"


async def main() -> int:
    load_dotenv()
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        print("GOOGLE_API_KEY missing")
        return 1

    print("=" * 78)
    print("LLM CANDIDATES")
    print("=" * 78)
    for model in LLM_CANDIDATES:
        report = await try_llm(api_key, model)
        print(f"\n{report['model']}")
        for key in ("complete", "complete_ms", "stream", "structured", "structured_ms"):
            if key in report:
                print(f"    {key:<15} {report[key]}")
        for key in ("people", "places", "phrase"):
            if key in report:
                print(f"    {key:<15} {report[key]}")

    print("\n" + "=" * 78)
    print("SMALL / CLASSIFIER CANDIDATES")
    print("=" * 78)
    for model in SMALL_CANDIDATES:
        report = await try_llm(api_key, model)
        print(f"\n{report['model']}")
        for key in ("complete", "structured", "structured_ms"):
            if key in report:
                print(f"    {key:<15} {report[key]}")

    print("\n" + "=" * 78)
    print("EMBEDDING CANDIDATES")
    print("=" * 78)
    for model in EMBED_CANDIDATES:
        print(f"  {model:<28} {await try_embedding(api_key, model)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
