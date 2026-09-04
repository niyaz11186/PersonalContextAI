"""RESILIENCY-10 — every outbound call is bounded in time and in concurrency.

The Step 6b changes were shipped without these tests, which the Unit 5 plan flagged
as "repeating the original mistake in a different place". The original mistake was
`services.md` specifying a provider semaphore during Inception that was never built,
and staying invisible for four units because every model call sat on the request path
and was therefore serialised by one user typing. Unit 5's background extraction
removes that accidental limit.

Two things this file is careful about:

**The bound is asserted by observing peak concurrency, not by checking the semaphore
exists.** `assert adapter._gate is not None` would pass against code that acquires the
slot and immediately releases it. `assert peak <= limit` alone is also insufficient —
it passes against code that accidentally serialises everything, which would be a
performance defect masquerading as compliance. So the tests assert peak equals the
limit when there is enough work to saturate it, AND that a higher limit produces
higher observed concurrency. Together those pin the semaphore as load-bearing.

**A timeout must be retryable.** Without that branch an explicit timeout is strictly
worse than none: it would fail calls that the pre-existing backoff would have
recovered. That is asserted directly rather than assumed from reading `_with_retry`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from pydantic import BaseModel

from pca.adapters.gemini.provider import GeminiProviderAdapter
from pca.adapters.graphiti.memory_graph import GraphitiMemoryAdapter
from pca.domain.conversation import Episode
from pca.domain.errors import MemoryGraphUnavailable, ProviderUnavailable
from pca.domain.ids import ConversationId, EpisodeId, MessageId
from pca.ports.llm import Prompt, PromptMessage

from datetime import UTC, datetime
from uuid import uuid4

ANCHOR = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------- doubles


class Tracker:
    """Records concurrent occupancy and the high-water mark."""

    def __init__(self) -> None:
        self.current = 0
        self.peak = 0
        self.attempts = 0

    def enter(self) -> None:
        self.attempts += 1
        self.current += 1
        self.peak = max(self.peak, self.current)

    def leave(self) -> None:
        self.current -= 1


class _Response:
    def __init__(self, text: str = "ok") -> None:
        self.text = text
        self.parsed = None


class _StreamChunk:
    def __init__(self, text: str) -> None:
        self.text = text


class _Models:
    """Stands in for `client.aio.models`.

    `hang=True` never returns, which is the unbounded-wait condition RESILIENCY-10
    forbids — the SDK's own default has no ceiling, so before Step 6b this is exactly
    what a stalled Gemini call did to the caller.
    """

    def __init__(
        self,
        tracker: Tracker,
        delay: float = 0.02,
        hang: bool = False,
        raise_with: Exception | None = None,
    ) -> None:
        self._tracker = tracker
        self._delay = delay
        self._hang = hang
        self._raise = raise_with

    async def _work(self) -> None:
        self._tracker.enter()
        try:
            if self._hang:
                await asyncio.sleep(3600)
            await asyncio.sleep(self._delay)
            if self._raise is not None:
                raise self._raise
        finally:
            self._tracker.leave()

    async def generate_content(self, **kwargs: Any) -> _Response:
        await self._work()
        return _Response()

    async def generate_content_stream(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        await self._work()

        async def _iter():  # type: ignore[no-untyped-def]
            yield _StreamChunk("hello ")
            yield _StreamChunk("world")

        return _iter()


class FakeGenAIClient:
    def __init__(self, models: _Models) -> None:
        class _Aio:
            def __init__(self, m: _Models) -> None:
                self.models = m

        self.aio = _Aio(models)


def adapter(
    tracker: Tracker,
    *,
    max_concurrency: int = 2,
    timeout: float = 5.0,
    delay: float = 0.02,
    hang: bool = False,
    raise_with: Exception | None = None,
) -> GeminiProviderAdapter:
    return GeminiProviderAdapter(
        api_key="unused",
        default_model="m",
        client=FakeGenAIClient(  # type: ignore[arg-type]
            _Models(tracker, delay=delay, hang=hang, raise_with=raise_with)
        ),
        max_concurrency=max_concurrency,
        timeout_seconds=timeout,
    )


def prompt() -> Prompt:
    return Prompt(messages=[PromptMessage(role="user", content="hi")])


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the retry backoff.

    A full timeout-retry cycle sleeps 0.6 + 1.2 + 2.4 s by design. That is correct
    behaviour and wrong for a unit test, so it is neutralised here — except in the
    one test that measures backoff, which restores a known delay locally.
    """
    monkeypatch.setattr(GeminiProviderAdapter, "_backoff", staticmethod(lambda _a: 0.0))


# ------------------------------------------------------- concurrency bounding


async def test_concurrent_llm_calls_never_exceed_the_configured_bound() -> None:
    """The bulkhead, asserted by observation.

    Twelve callers against a bound of 3. If the semaphore were absent, decorative, or
    released too early, peak occupancy would climb toward 12.
    """
    tracker = Tracker()
    provider = adapter(tracker, max_concurrency=3, delay=0.05)

    await asyncio.gather(*(provider.complete(prompt()) for _ in range(12)))

    assert tracker.peak <= 3, f"bulkhead breached: peak {tracker.peak} exceeded 3"
    assert tracker.attempts == 12, "every call should still have been made"


async def test_the_bound_actually_saturates_rather_than_serialising() -> None:
    """`peak <= limit` alone would pass against code that runs everything one at a
    time. That would be a performance defect wearing compliance as a disguise, so the
    bound is also asserted to be *reached*."""
    tracker = Tracker()
    provider = adapter(tracker, max_concurrency=4, delay=0.05)

    await asyncio.gather(*(provider.complete(prompt()) for _ in range(12)))

    assert tracker.peak == 4, (
        f"expected the bulkhead to saturate at 4, observed peak {tracker.peak} — "
        "a lower peak means calls are being serialised rather than bounded"
    )


async def test_raising_the_bound_raises_observed_concurrency() -> None:
    """Proves the assertions above are not vacuous.

    If the semaphore were ignored entirely, both configurations would behave
    identically and this test would fail — which is what makes the peak assertions
    meaningful rather than coincidental.
    """
    tight, loose = Tracker(), Tracker()

    # One adapter instance per batch: the semaphore is per-adapter, so building a new
    # one per call would give every call its own gate and measure nothing.
    tight_provider = adapter(tight, max_concurrency=2, delay=0.05)
    loose_provider = adapter(loose, max_concurrency=8, delay=0.05)

    await asyncio.gather(*(tight_provider.complete(prompt()) for _ in range(10)))
    await asyncio.gather(*(loose_provider.complete(prompt()) for _ in range(10)))

    assert tight.peak < loose.peak, (
        f"tight bound {tight.peak} should observe less concurrency than loose "
        f"{loose.peak}; equal values mean the semaphore is not in effect"
    )


async def test_in_flight_returns_to_zero_after_a_burst() -> None:
    """A leaked slot is invisible until the pool is exhausted, at which point the
    system stalls with no error to point at."""
    tracker = Tracker()
    provider = adapter(tracker, max_concurrency=2)

    await asyncio.gather(*(provider.complete(prompt()) for _ in range(6)))

    assert provider.in_flight == 0


async def test_a_failed_call_still_releases_its_slot() -> None:
    """The failure path is where slot leaks actually happen."""
    tracker = Tracker()
    provider = adapter(
        tracker, max_concurrency=2, raise_with=ValueError("bad request")
    )

    with pytest.raises(ProviderUnavailable):
        await provider.complete(prompt())

    assert provider.in_flight == 0


# ------------------------------------------------------------------- timeouts


async def test_a_hung_llm_call_is_cut_off_rather_than_waited_on() -> None:
    """The core RESILIENCY-10 requirement.

    `_with_retry` previously matched a timeout marker in the exception text but never
    imposed a timeout of its own, so a hung SDK call waited on the SDK's default —
    effectively forever.
    """
    tracker = Tracker()
    provider = adapter(tracker, timeout=0.05, hang=True)

    started = time.perf_counter()
    with pytest.raises(ProviderUnavailable, match="failed after"):
        await provider.complete(prompt())
    elapsed = time.perf_counter() - started

    assert elapsed < 5.0, f"call was not bounded; waited {elapsed:.1f}s"


async def test_a_timeout_is_retried_rather_than_failing_immediately() -> None:
    """Without this branch an explicit timeout would be strictly worse than none.

    Four attempts, matching `_MAX_ATTEMPTS`. A timeout is the transient case by
    definition, so it must go through the same backoff as a 503.
    """
    tracker = Tracker()
    provider = adapter(tracker, timeout=0.02, hang=True)

    with pytest.raises(ProviderUnavailable):
        await provider.complete(prompt())

    assert tracker.attempts == 4, (
        f"expected 4 attempts on repeated timeout, saw {tracker.attempts}"
    )


async def test_a_non_retryable_error_is_not_retried() -> None:
    """The timeout branch must not have turned every failure into four attempts —
    retrying a malformed request just spends the latency budget to fail again."""
    tracker = Tracker()
    provider = adapter(tracker, raise_with=ValueError("invalid argument"))

    with pytest.raises(ProviderUnavailable):
        await provider.complete(prompt())

    assert tracker.attempts == 1


async def test_a_retryable_error_is_retried() -> None:
    tracker = Tracker()
    provider = adapter(tracker, raise_with=RuntimeError("429 rate limit exceeded"))

    with pytest.raises(ProviderUnavailable):
        await provider.complete(prompt())

    assert tracker.attempts == 4


async def test_the_slot_is_released_before_the_backoff_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Holding a slot across an 8 s sleep would starve the bulkhead it protects.

    With a bound of 1, a stalled-and-retrying caller must not block an unrelated
    healthy caller for the duration of its backoff. Asserted by timing the healthy
    call, because the alternative — inspecting semaphore internals — would pass
    against code that releases the slot a moment too late.
    """
    # A long backoff deliberately. The assertion below is that a healthy call is not
    # made to wait for it, and a generous gap between the two makes that robust under
    # load — an earlier 0.25s backoff with a 0.2s threshold passed in isolation and
    # failed inside the full suite, which is a flaky test rather than a real signal.
    backoff_seconds = 3.0
    monkeypatch.setattr(
        GeminiProviderAdapter, "_backoff", staticmethod(lambda _a: backoff_seconds)
    )

    hung_tracker = Tracker()
    provider = GeminiProviderAdapter(
        api_key="unused",
        default_model="m",
        client=FakeGenAIClient(_Models(hung_tracker, hang=True)),  # type: ignore[arg-type]
        max_concurrency=1,
        timeout_seconds=0.02,
    )

    failing = asyncio.create_task(provider.complete(prompt()))
    # Let the first attempt time out and enter backoff.
    await asyncio.sleep(0.08)

    # Swap in a healthy backend; the semaphore is per-adapter, so the same gate is
    # shared with the task now sleeping in backoff.
    healthy_tracker = Tracker()
    provider._client = FakeGenAIClient(_Models(healthy_tracker, delay=0.01))  # type: ignore[assignment]

    started = time.perf_counter()
    await provider.complete(prompt())
    elapsed = time.perf_counter() - started

    assert elapsed < backoff_seconds / 2, (
        f"healthy call waited {elapsed:.3f}s against a {backoff_seconds}s backoff — "
        "the retrying call is holding the bulkhead slot across its sleep"
    )

    # Cancelled rather than awaited. Its outcome is incidental to what this test
    # measures, and letting it finish would sit through three more 3-second backoffs —
    # a slow test earning nothing.
    failing.cancel()
    await asyncio.gather(failing, return_exceptions=True)


async def test_stream_bounds_establishing_the_stream() -> None:
    """A long stream is legitimate; one that never yields a first chunk is not.

    The timeout therefore covers establishment only — bounding consumption would cut
    off perfectly healthy long replies.
    """
    tracker = Tracker()
    provider = adapter(tracker, timeout=0.05, hang=True)

    with pytest.raises(ProviderUnavailable, match="did not start"):
        async for _ in provider.stream(prompt()):
            pass


async def test_a_healthy_stream_is_not_cut_off() -> None:
    tracker = Tracker()
    provider = adapter(tracker, timeout=5.0, delay=0.01)

    chunks = [chunk async for chunk in provider.stream(prompt())]

    assert "".join(chunks) == "hello world"


async def test_structured_output_is_bounded_too() -> None:
    """Extraction runs through `structured`, so an unbounded call here stalls the
    background pipeline rather than a visible request."""

    class _Schema(BaseModel):
        value: str = "x"

    tracker = Tracker()
    provider = adapter(tracker, timeout=0.05, hang=True)

    with pytest.raises(ProviderUnavailable):
        await provider.structured(prompt(), _Schema)


# ------------------------------------------------------- graph adapter bounds


class HangingGraphiti:
    """Graphiti stub whose every call never returns."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def add_episode(self, **kwargs: Any) -> Any:
        self.calls.append("add_episode")
        await asyncio.sleep(3600)

    async def search_(self, **kwargs: Any) -> Any:
        self.calls.append("search_")
        await asyncio.sleep(3600)


def graph_adapter(stub: Any, timeout: float = 0.05) -> GraphitiMemoryAdapter:
    return GraphitiMemoryAdapter(
        uri="bolt://unused",
        user="neo4j",
        password="unused",
        api_key="unused",
        llm_model="m",
        small_model="s",
        embedding_model="e",
        reranker_model="r",
        graphiti=stub,
        timeout_seconds=timeout,
    )


def episode() -> Episode:
    return Episode(
        id=EpisodeId(uuid4()),
        content="My sister Priya lives in Pune.",
        occurred_at=ANCHOR,
        zone="Asia/Kolkata",
        conversation_id=ConversationId(uuid4()),
        message_id=MessageId(uuid4()),
    )


async def test_a_hung_graph_ingestion_is_cut_off() -> None:
    """The adapter previously had no timeout of any kind.

    The retrieval budget governor masked this on the read path — it stops waiting on
    its own schedule. Nothing masked the write path, so a hung Neo4j call blocked
    ingestion indefinitely while `/health` still reported 200.
    """
    stub = HangingGraphiti()

    started = time.perf_counter()
    with pytest.raises(MemoryGraphUnavailable):
        await graph_adapter(stub).add_episode(episode())
    elapsed = time.perf_counter() - started

    assert elapsed < 5.0, f"ingestion was not bounded; waited {elapsed:.1f}s"


async def test_a_hung_graph_search_is_cut_off() -> None:
    stub = HangingGraphiti()

    with pytest.raises(MemoryGraphUnavailable):
        await graph_adapter(stub).search_semantic("Priya", limit=5)


async def test_graph_timeouts_surface_as_the_domain_error_callers_degrade_on() -> None:
    """ADR-005 makes Neo4j a rebuildable projection with a degradation path, so a
    timeout must arrive as `MemoryGraphUnavailable` — the type retrieval already
    catches — rather than as a bare `TimeoutError` that would escape as a 500."""
    stub = HangingGraphiti()

    with pytest.raises(MemoryGraphUnavailable, match="exceeded"):
        await graph_adapter(stub).search_fulltext("Priya", limit=5)


async def test_every_graph_search_path_is_guarded() -> None:
    """`_guard` is applied at seven call sites. A single unguarded path is enough to
    reintroduce the unbounded wait, and it would only surface under the exact failure
    this bounds — so each entry point is checked rather than sampled."""
    stub = HangingGraphiti()
    adapter_ = graph_adapter(stub)

    for call in (
        adapter_.search_semantic("q", 5),
        adapter_.search_fulltext("q", 5),
        adapter_.search_temporal("q", (ANCHOR, ANCHOR), 5),
        adapter_.traverse("q", "node-1", 2),
    ):
        with pytest.raises(MemoryGraphUnavailable):
            await call
