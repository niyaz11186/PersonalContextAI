"""ExtractionCoordinator — the ADR-008 write barrier.

This is the component that retires the NFR-02.3 exception carried since Unit 1b, so
the tests that matter most are the ones that would still pass against the old
synchronous code if written carelessly. Two guard against exactly that:

  * `test_submit_returns_before_the_extraction_finishes` fails if extraction is
    awaited inline, which is what Unit 1b did.
  * `test_a_slow_conversation_does_not_delay_another` fails against a global lock,
    which is the tempting simplification ADR-008 explicitly rules out (C-32).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from pca.domain.enums import ExtractionState
from pca.domain.ids import ConversationId, EpisodeId
from pca.domain.orchestration import ExtractionOutcome
from pca.services.degradation import DegradationPolicy
from pca.services.extraction_coordinator import ExtractionCoordinator
from tests.fakes.clock import FakeClock
from tests.fakes.extraction_status import FakeExtractionStatusRepository

pytestmark = pytest.mark.asyncio


def _episode() -> EpisodeId:
    return EpisodeId(uuid4())


def _conversation() -> ConversationId:
    return ConversationId(uuid4())


def _build(
    runner,
    *,
    max_concurrent: int = 2,
    extraction_timeout: timedelta = timedelta(seconds=5),
    barrier_timeout: timedelta = timedelta(seconds=5),
) -> tuple[ExtractionCoordinator, FakeExtractionStatusRepository]:
    repository = FakeExtractionStatusRepository()
    coordinator = ExtractionCoordinator(
        repository=repository,
        clock=FakeClock(),
        runner=runner,
        degradation=DegradationPolicy(),
        barrier_timeout=barrier_timeout,
        extraction_timeout=extraction_timeout,
        max_concurrent=max_concurrent,
    )
    return coordinator, repository


async def _ok(_episode_id: EpisodeId) -> ExtractionOutcome:
    return ExtractionOutcome(
        episode_id=_episode_id, state=ExtractionState.SUCCEEDED, facts_committed=1
    )


# ------------------------------------------------------------------ NFR-02.3


async def test_submit_returns_before_the_extraction_finishes() -> None:
    """The whole point of the unit. Fails against Unit 1b's synchronous path."""
    release = asyncio.Event()

    async def slow(episode_id: EpisodeId) -> ExtractionOutcome:
        await release.wait()
        return await _ok(episode_id)

    coordinator, _ = _build(slow)
    episode = _episode()

    await coordinator.submit(episode, _conversation())

    assert coordinator.in_flight == 1, "submit awaited the extraction instead of queuing it"

    release.set()
    await coordinator.drain()


async def test_a_slow_conversation_does_not_delay_another() -> None:
    """C-32: the barrier is per-conversation, never global."""
    release = asyncio.Event()
    slow_episode = _episode()

    async def runner(episode_id: EpisodeId) -> ExtractionOutcome:
        if episode_id == slow_episode:
            await release.wait()
        return await _ok(episode_id)

    coordinator, _ = _build(runner)
    slow_conversation, fast_conversation = _conversation(), _conversation()

    await coordinator.submit(slow_episode, slow_conversation)
    await coordinator.submit(_episode(), fast_conversation)
    await asyncio.sleep(0)  # let both tasks start

    # The fast conversation clears while the slow one is still blocked.
    fast = await coordinator.await_barrier(
        fast_conversation, timeout=timedelta(seconds=2)
    )
    assert fast.cleared

    release.set()
    await coordinator.drain()


# ------------------------------------------------------------------- barrier


async def test_the_barrier_waits_for_that_conversations_extraction() -> None:
    release = asyncio.Event()

    async def slow(episode_id: EpisodeId) -> ExtractionOutcome:
        await release.wait()
        return await _ok(episode_id)

    coordinator, _ = _build(slow)
    conversation = _conversation()
    await coordinator.submit(_episode(), conversation)

    barrier = asyncio.create_task(coordinator.await_barrier(conversation))
    await asyncio.sleep(0)
    assert not barrier.done(), "barrier cleared while extraction was still running"

    release.set()
    assert (await barrier).cleared


async def test_a_timed_out_barrier_discloses_rather_than_raising() -> None:
    """ADR-008 and NFR-06.5: proceed with a disclosure, never block forever.

    Raising here would lose the user's turn over work they never asked for.
    """
    release = asyncio.Event()

    async def slow(episode_id: EpisodeId) -> ExtractionOutcome:
        await release.wait()
        return await _ok(episode_id)

    coordinator, _ = _build(slow, barrier_timeout=timedelta(milliseconds=30))
    conversation = _conversation()
    await coordinator.submit(_episode(), conversation)

    result = await coordinator.await_barrier(conversation)

    assert not result.cleared
    assert result.timed_out
    assert result.pending_episodes == 1
    assert result.degradation is not None
    assert result.degradation.disclosure.strip(), "degraded without telling the user"

    release.set()
    await coordinator.drain()


async def test_the_barrier_is_free_when_nothing_is_pending() -> None:
    coordinator, _ = _build(_ok)
    result = await coordinator.await_barrier(_conversation())
    assert result.cleared
    assert result.waited == timedelta(0)


# -------------------------------------------------------------- idempotency


async def test_a_duplicate_submit_is_a_no_op() -> None:
    """C-35. A retried submit must not run extraction twice and double-write facts."""
    runs: list[EpisodeId] = []

    async def counting(episode_id: EpisodeId) -> ExtractionOutcome:
        runs.append(episode_id)
        return await _ok(episode_id)

    coordinator, _ = _build(counting)
    episode, conversation = _episode(), _conversation()

    assert await coordinator.submit(episode, conversation) is True
    assert await coordinator.submit(episode, conversation) is False

    await coordinator.drain()
    assert runs == [episode]


# ------------------------------------------------------------ bounded pool


async def test_concurrent_extraction_never_exceeds_the_configured_bound() -> None:
    """RESILIENCY-10 bulkhead.

    Asserts *observed peak concurrency*, not the presence of a semaphore. An
    implementation that constructed a semaphore and never awaited it would pass the
    weaker check while leaving the failure mode fully intact.
    """
    active = 0
    peak = 0
    release = asyncio.Event()

    async def tracked(episode_id: EpisodeId) -> ExtractionOutcome:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()
        active -= 1
        return await _ok(episode_id)

    coordinator, _ = _build(tracked, max_concurrent=2)
    for _ in range(6):
        await coordinator.submit(_episode(), _conversation())

    await asyncio.sleep(0.05)
    assert peak <= 2, f"{peak} extractions ran at once against a bound of 2"

    release.set()
    await coordinator.drain()
    assert peak <= 2


# ---------------------------------------------------------------- outcomes


async def test_a_failed_extraction_is_recorded_not_raised() -> None:
    async def failing(_episode_id: EpisodeId) -> ExtractionOutcome:
        raise RuntimeError("gemini exploded")

    coordinator, repository = _build(failing)
    episode = _episode()
    await coordinator.submit(episode, _conversation())
    await coordinator.drain()

    record = await repository.get(episode)
    assert record is not None
    assert record.state is ExtractionState.FAILED
    assert "gemini exploded" in (record.error or "")


async def test_a_hung_extraction_is_abandoned_not_failed() -> None:
    """The distinction that keeps recovery honest.

    FAILED means it ran and could not finish. ABANDONED means it ran long and is
    still owed a retry, so `recoverable()` includes it. Collapsing the two would
    either retry genuine failures forever or discard work that was merely slow.
    """
    async def hangs(_episode_id: EpisodeId) -> ExtractionOutcome:
        await asyncio.sleep(10)
        raise AssertionError("should have been cut off")

    coordinator, repository = _build(
        hangs, extraction_timeout=timedelta(milliseconds=30)
    )
    episode = _episode()
    await coordinator.submit(episode, _conversation())
    await coordinator.drain()

    record = await repository.get(episode)
    assert record is not None
    assert record.state is ExtractionState.ABANDONED
    assert record in await repository.recoverable()


async def test_a_hung_extraction_releases_its_pool_slot() -> None:
    """Without a task-side timeout the slot is held forever.

    The barrier timeout unblocks the *reader*; it does nothing about the extraction
    itself. A hung task with no timeout would occupy a slot permanently, and enough
    of them would stop all extraction while /health still reported 200.
    """
    async def hangs(_episode_id: EpisodeId) -> ExtractionOutcome:
        await asyncio.sleep(10)
        raise AssertionError("should have been cut off")

    coordinator, _ = _build(
        hangs, max_concurrent=1, extraction_timeout=timedelta(milliseconds=20)
    )
    await coordinator.submit(_episode(), _conversation())

    ran = asyncio.Event()

    async def quick(episode_id: EpisodeId) -> ExtractionOutcome:
        ran.set()
        return await _ok(episode_id)

    coordinator._runner = quick  # noqa: SLF001 - swapping the injected runner
    await coordinator.submit(_episode(), _conversation())

    await asyncio.wait_for(ran.wait(), timeout=2)
    await coordinator.drain()


# ---------------------------------------------------------------- recovery


async def test_recover_pending_requeues_work_left_by_a_crash() -> None:
    coordinator, repository = _build(_ok)
    stranded = _episode()

    # A row left RUNNING with no local task: the shape a crash leaves behind.
    await repository.claim(stranded, _conversation(), FakeClock().now())
    await repository.mark_running(stranded, FakeClock().now())

    requeued = await coordinator.recover_pending()
    await coordinator.drain()

    assert requeued == [stranded]
    record = await repository.get(stranded)
    assert record is not None
    assert record.state is ExtractionState.SUCCEEDED


async def test_recovery_survives_an_episode_that_fails_again() -> None:
    """Partial recovery must not abort the rest.

    Startup calls this. Raising would make one poisonous episode enough to render the
    whole application unusable, when it could run with a visible backlog instead.
    """
    bad = _episode()

    async def selective(episode_id: EpisodeId) -> ExtractionOutcome:
        if episode_id == bad:
            raise RuntimeError("still broken")
        return await _ok(episode_id)

    coordinator, repository = _build(selective)
    good = _episode()
    for episode in (bad, good):
        await repository.claim(episode, _conversation(), FakeClock().now())

    requeued = await coordinator.recover_pending()
    await coordinator.drain()

    assert set(requeued) == {bad, good}
    assert (await repository.get(good)).state is ExtractionState.SUCCEEDED
    assert (await repository.get(bad)).state is ExtractionState.FAILED


# ------------------------------------------------------------- lifecycle


async def test_quiesce_holds_new_work_and_resume_releases_it() -> None:
    """ADR-013: backup pauses the coordinator so the episode log has no gaps."""
    started = asyncio.Event()

    async def signalling(episode_id: EpisodeId) -> ExtractionOutcome:
        started.set()
        return await _ok(episode_id)

    coordinator, _ = _build(signalling)
    coordinator.quiesce()
    await coordinator.submit(_episode(), _conversation())
    await asyncio.sleep(0.02)

    assert not started.is_set(), "extraction started while quiesced"

    coordinator.resume()
    await asyncio.wait_for(started.wait(), timeout=2)
    await coordinator.drain()


async def test_backlog_reports_state_counts_for_health() -> None:
    coordinator, _ = _build(_ok)
    await coordinator.submit(_episode(), _conversation())
    await coordinator.drain()

    backlog = await coordinator.backlog()
    assert backlog[ExtractionState.SUCCEEDED.value] == 1
    assert backlog["in_flight_local"] == 0
