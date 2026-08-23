"""Verify small/classifier models and embedding models."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel

sys.path.insert(0, "src")

from pca.adapters.gemini.provider import GeminiProviderAdapter  # noqa: E402
from pca.ports.llm import Prompt, PromptMessage  # noqa: E402

SAMPLE = "I had a big argument with my sister Priya last Tuesday about the house in Pune."


class Intent(BaseModel):
    """Small-model workload: routing classification (IntentRouter, Unit 5)."""

    intent: Literal["conversation", "new_information", "correction", "historical", "ambiguous"]
    confidence: float


async def check_small(api_key: str, model: str) -> None:
    adapter = GeminiProviderAdapter(api_key=api_key, default_model=model)
    try:
        started = time.perf_counter()
        result = await adapter.structured(
            Prompt(
                system="Classify the user's intent.",
                messages=[PromptMessage(role="user", content=SAMPLE)],
            ),
            Intent,
        )
        ms = round((time.perf_counter() - started) * 1000)
        print(f"  {model:<28} OK  {ms:>6} ms  -> {result.intent} ({result.confidence:.2f})")
    except Exception as exc:
        print(f"  {model:<28} FAIL {type(exc).__name__}: {str(exc)[:100]}")


async def check_embed(api_key: str, model: str) -> None:
    from google import genai

    client = genai.Client(api_key=api_key)
    try:
        started = time.perf_counter()
        response = await client.aio.models.embed_content(model=model, contents=SAMPLE)
        ms = round((time.perf_counter() - started) * 1000)
        values = response.embeddings[0].values or []
        print(f"  {model:<28} OK  {ms:>6} ms  -> dimension {len(values)}")
    except Exception as exc:
        print(f"  {model:<28} FAIL {type(exc).__name__}: {str(exc)[:100]}")


async def main() -> int:
    load_dotenv()
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        print("GOOGLE_API_KEY missing")
        return 1

    print("SMALL / CLASSIFIER MODELS")
    for model in ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"]:
        await check_small(api_key, model)

    print("\nEMBEDDING MODELS")
    for model in ["gemini-embedding-001", "gemini-embedding-2"]:
        await check_embed(api_key, model)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
