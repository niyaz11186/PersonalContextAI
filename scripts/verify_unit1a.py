"""Unit 1a completion check.

Exercises every component Unit 1a delivers, end to end, with no database:
configuration, clock, TimeResolver, and the live Gemini adapter using the pinned
models. Also runs the ADR-010 contract for real — Gemini returns the structure of
a time phrase and TimeResolver computes the date.

Run:  .\\venv\\Scripts\\python.exe scripts\\verify_unit1a.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

sys.path.insert(0, "src")

from pca.adapters.clock.system_clock import SystemClockAdapter  # noqa: E402
from pca.adapters.gemini.provider import GeminiProviderAdapter  # noqa: E402
from pca.config.settings import get_settings  # noqa: E402
from pca.domain.enums import (  # noqa: E402
    Granularity,
    ResolutionMethod,
    TemporalDirection,
    TemporalModifier,
    TemporalUnit,
)
from pca.domain.temporal import RelativeDescriptor, TemporalExpression  # noqa: E402
from pca.observability.logging import configure_logging, new_correlation_id  # noqa: E402
from pca.ports.llm import Prompt, PromptMessage  # noqa: E402
from pca.services.time_resolver import TimeResolver  # noqa: E402

SAMPLE = "I had a big argument with my sister Priya last Tuesday about the house in Pune."

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))


class TimePhrase(BaseModel):
    raw_phrase: str
    direction: Literal["past", "future", "none"]
    quantity: int | None = None
    unit: Literal["day", "week", "month", "quarter", "year"] | None = None
    weekday: int | None = Field(default=None, description="0=Monday..6=Sunday")
    modifier: Literal["last", "this", "next"] | None = None


class Extraction(BaseModel):
    people: list[str]
    places: list[str]
    time_phrases: list[TimePhrase]


async def main() -> int:
    configure_logging("WARNING")
    cid = new_correlation_id()

    # 1. configuration
    try:
        settings = get_settings()
        settings.require_runtime_secrets()
        record(
            "config loads and validates",
            True,
            f"llm={settings.llm_model} embed={settings.embedding_model} tz={settings.user_timezone}",
        )
    except Exception as exc:
        record("config loads and validates", False, str(exc)[:120])
        _report()
        return 1

    # 2. clock
    clock = SystemClockAdapter(zone=settings.user_timezone)
    now = clock.now()
    record(
        "clock returns aware UTC + zone",
        now.tzinfo is not None and clock.zone() == settings.user_timezone,
        f"{now.isoformat()} zone={clock.zone()}",
    )

    # 3. TimeResolver in isolation
    resolver = TimeResolver()
    anchor = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)  # a Thursday
    start, end, granularity = resolver.resolve(
        RelativeDescriptor(weekday=1, modifier=TemporalModifier.LAST),
        anchor,
        settings.user_timezone,
    )
    record(
        "TimeResolver: 'last Tuesday' in Asia/Kolkata",
        granularity is Granularity.DAY and start is not None,
        f"{start} -> {end} ({granularity})",
    )

    # 4. unresolvable stays unresolvable
    s2, e2, g2 = resolver.resolve(RelativeDescriptor(), anchor, settings.user_timezone)
    record(
        "TimeResolver: unresolvable yields UNKNOWN with null dates",
        g2 is Granularity.UNKNOWN and s2 is None and e2 is None,
    )

    provider = GeminiProviderAdapter(
        api_key=settings.google_api_key,
        default_model=settings.llm_model,
        small_model=settings.llm_small_model,
    )

    # 5. completion
    try:
        started = time.perf_counter()
        text = await provider.complete(
            Prompt(
                system="Answer in exactly one word.",
                messages=[PromptMessage(role="user", content="What is the capital of France?")],
            )
        )
        record("Gemini complete", bool(text.strip()),
               f"{text.strip()[:30]!r} in {round((time.perf_counter()-started)*1000)}ms")
    except Exception as exc:
        record("Gemini complete", False, str(exc)[:120])

    # 6. streaming (FR-01.2 / SSE path)
    try:
        chunks = [c async for c in provider.stream(
            Prompt(messages=[PromptMessage(role="user", content="List three colours.")])
        )]
        record("Gemini stream", bool(chunks), f"{len(chunks)} chunks")
    except Exception as exc:
        record("Gemini stream", False, str(exc)[:120])

    # 7. structured output
    extraction: Extraction | None = None
    try:
        started = time.perf_counter()
        extraction = await provider.structured(
            Prompt(
                system=(
                    "Extract people, places and time phrases. For each time phrase return "
                    "its STRUCTURE only (direction, quantity, unit, weekday, modifier). "
                    "Never compute or return an actual date."
                ),
                messages=[PromptMessage(role="user", content=SAMPLE)],
            ),
            Extraction,
        )
        ms = round((time.perf_counter() - started) * 1000)
        ok = "Priya" in extraction.people and "Pune" in extraction.places
        record("Gemini structured extraction", ok,
               f"people={extraction.people} places={extraction.places} in {ms}ms")
        record("structured latency within 25s budget", ms < 25_000, f"{ms}ms")
    except Exception as exc:
        record("Gemini structured extraction", False, str(exc)[:120])

    # 8. the ADR-010 contract end to end:
    #    model supplies structure, our deterministic code supplies the date.
    if extraction and extraction.time_phrases:
        phrase = extraction.time_phrases[0]
        descriptor = RelativeDescriptor(
            direction=TemporalDirection(phrase.direction),
            quantity=phrase.quantity,
            unit=TemporalUnit(phrase.unit) if phrase.unit else None,
            weekday=phrase.weekday,
            modifier=TemporalModifier(phrase.modifier) if phrase.modifier else None,
        )
        rf, rt, g = resolver.resolve(descriptor, anchor, settings.user_timezone)
        expression = TemporalExpression(
            raw_phrase=phrase.raw_phrase,
            descriptor=descriptor,
            resolved_from=rf,
            resolved_to=rt,
            granularity=g,
            method=(
                ResolutionMethod.CLOCK_RELATIVE
                if g is not Granularity.UNKNOWN
                else ResolutionMethod.UNRESOLVED
            ),
            anchor_zone=settings.user_timezone,
        )
        record(
            "ADR-010 contract: model parses, code computes",
            expression.is_resolved and expression.raw_phrase == phrase.raw_phrase,
            f"{expression.raw_phrase!r} -> {rf} .. {rt} ({g})",
        )
        record(
            "raw phrase retained through resolution",
            bool(expression.raw_phrase),
            expression.raw_phrase,
        )
    else:
        record("ADR-010 contract: model parses, code computes", False, "no time phrase extracted")

    # 9. embeddings
    try:
        from google import genai

        client = genai.Client(api_key=settings.google_api_key)
        response = await client.aio.models.embed_content(
            model=settings.embedding_model, contents=SAMPLE
        )
        dim = len(response.embeddings[0].values or [])
        record("Gemini embeddings", dim > 0, f"{settings.embedding_model} dimension={dim}")
    except Exception as exc:
        record("Gemini embeddings", False, str(exc)[:120])

    # 10. provider health
    try:
        health = await provider.health()
        record("provider health check", health.healthy, f"model={health.model}")
    except Exception as exc:
        record("provider health check", False, str(exc)[:120])

    return _report(cid)


def _report(cid: str = "") -> int:
    print("\n" + "=" * 78)
    print(f"UNIT 1a VERIFICATION{('  correlation=' + cid) if cid else ''}")
    print("=" * 78)
    for status, name, detail in results:
        print(f"  [{status}] {name}")
        if detail:
            print(f"         {detail}")
    failures = sum(1 for s, _, _ in results if s == FAIL)
    print("-" * 78)
    print(f"  {len(results) - failures}/{len(results)} passed")
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
