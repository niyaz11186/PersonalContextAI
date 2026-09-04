"""ExtractionCoordinator — the ADR-008 write barrier.

Layer L3.

This is the component that retires the NFR-02.3 exception carried since Unit 1b.
Until now extraction ran synchronously inside the SSE response: correct, but it made
the user wait for work they did not ask for.

ADR-008 resolves the conflict between two requirements that pull opposite ways.
NFR-02.3 says extraction must not block the response. The core product hypothesis
says a fact stated now must be retrievable later — and "later" includes the very next
message. So: respond immediately, extract in the background, and before processing
the *next* message in that conversation, wait for the previous extraction to finish.

    Message N   arrives -> barrier clear -> respond -> spawn E(N)
    Message N+1 arrives -> barrier held by E(N) -> await E(N) -> respond -> spawn E(N+1)

Four constraints follow, and each is a separate way to get this wrong:

  per-conversation   A global lock would let one conversation's slow extraction delay
                     every other. The barrier is keyed by conversation (C-32).
  timeout            A hung Gemini call must not block the user forever. On timeout
                     we proceed with a disclosure (NFR-06.5), and the work stays
                     durable for `recover_pending`.
  durable            An in-process lock dies with the process. The `extraction_status`
                     row is the real record; the lock is an optimisation on top.
  idempotent         A retried extraction must not double-write facts. Enforced at the
                     database by the `episode_id` primary key (C-35), not here.

Bounded concurrency is a fifth, added by the Unit 5 resiliency review rather than by
ADR-008. Unbounded `create_task` lets a burst of messages spawn arbitrarily many
concurrent Gemini calls; exhausting the rate limit then times out *every*
conversation's barrier at once, so one saturated dependency stalls the whole write
path. The per-conversation barrier does not contain that — only a cap does.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import timedelta

from pca.domain.enums import ExtractionState
from pca.domain.ids import ConversationId, EpisodeId
from pca.domain.orchestration import BarrierResult, ExtractionOutcome
from pca.observability.logging import get_logger
from pca.ports.clock import ClockPort
from pca.ports.repositories import ExtractionStatusRepositoryPort
from pca.services.degradation import DegradationPolicy

_log = get_logger(__name__)

ExtractionRunner = Callable[[EpisodeId], Awaitable[ExtractionOutcome]]


class ExtractionCoordinator:
    def __init__(
        self,
        repository: ExtractionStatusRepositoryPort,
        clock: ClockPort,
        runner: ExtractionRunner,
        degradation: DegradationPolicy,
        barrier_timeout: timedelta = timedelta(seconds=60),
        extraction_timeout: timedelta = timedelta(seconds=300),
        max_concurrent: int = 2,
    ) -> None:
        self._repository = repository
        self._clock = clock
        # Injected rather than constructed: the coordinator owns scheduling, the
        # workflow owns the work. That split is also what lets these tests run
        # without an LLM.
        self._runner = runner
        self._degradation = degradation
        self._barrier_timeout = barrier_timeout
        self._extraction_timeout = extraction_timeout
        self._slots = asyncio.Semaphore(max_concurrent)

        # Strong references. asyncio only holds a weak reference to a running task,
        # so a task not kept somewhere can be garbage-collected mid-flight — the
        # extraction would simply stop, leaving a `running` row nobody retries until
        # the next restart.
        self._tasks: set[asyncio.Task[None]] = set()
        self._by_conversation: dict[ConversationId, set[asyncio.Task[None]]] = (
            defaultdict(set)
        )

        # Findings that must reach the user but were discovered after their reply had
        # already been sent.
        #
        # This exists because moving extraction off the response path would otherwise
        # silently drop two things the system is required to surface: contradictions
        # (FR-05.6 — surface, never resolve) and entity ambiguity (ADR-014). Those are
        # detected during extraction, which now finishes after the reply.
        #
        # They are delivered on the NEXT turn instead, which is exactly when the
        # barrier has guaranteed the extraction completed. One turn late is the honest
        # consequence of ADR-008's trade; dropping them would be a silent requirement
        # violation traded for latency.
        #
        # In-process and therefore lost on restart. Acceptable because these are
        # advisory: the underlying provisional entity and both conflicting facts are
        # durably stored, and Unit 6's inspection API surfaces them directly.
        self._notices: dict[ConversationId, list[str]] = defaultdict(list)

        # Set = open. Unit 7's backup quiesces the coordinator so the episode log has
        # no in-flight gaps while it runs.
        self._open = asyncio.Event()
        self._open.set()

    # ------------------------------------------------------------------ public

    async def submit(
        self, episode_id: EpisodeId, conversation_id: ConversationId | None
    ) -> bool:
        """Queue an episode for background extraction. False if already claimed.

        The durable row is written **before** the task is spawned. Reversed, a crash
        between the two would lose the episode with no record it was ever queued —
        and `recover_pending` only knows about what the table remembers.
        """
        claimed = await self._repository.claim(
            episode_id, conversation_id, self._clock.now()
        )
        if not claimed:
            return False

        task = asyncio.create_task(
            self._run(episode_id, conversation_id), name=f"extract-{episode_id}"
        )
        self._tasks.add(task)
        if conversation_id is not None:
            self._by_conversation[conversation_id].add(task)

        def _release(finished: asyncio.Task[None]) -> None:
            self._tasks.discard(finished)
            if conversation_id is not None:
                self._by_conversation[conversation_id].discard(finished)

        task.add_done_callback(_release)
        return True

    async def await_barrier(
        self, conversation_id: ConversationId, timeout: timedelta | None = None
    ) -> BarrierResult:
        """Wait for this conversation's pending extraction (ADR-008).

        Waits only on tasks owned by *this* process. A durable `running` row with no
        local task belongs to a process that died; waiting on it would block until the
        timeout every single time, when `recover_pending` at startup is the mechanism
        that actually resolves it.
        """
        limit = timeout or self._barrier_timeout
        started = self._clock.now()

        outstanding = {
            task for task in self._by_conversation.get(conversation_id, set())
            if not task.done()
        }
        if not outstanding:
            return BarrierResult(cleared=True, waited=timedelta(0))

        _, pending = await asyncio.wait(
            outstanding, timeout=limit.total_seconds()
        )
        waited = self._clock.now() - started

        if pending:
            return BarrierResult(
                cleared=False,
                waited=waited,
                pending_episodes=len(pending),
                # Carries the sentence the user must see. The extraction keeps
                # running; we simply stop making the reader wait for it.
                degradation=self._degradation.on_extraction_timeout(conversation_id),
            )

        return BarrierResult(cleared=True, waited=waited)

    async def recover_pending(self, limit: int = 100) -> list[EpisodeId]:
        """Re-queue work left unfinished by a crash. Called at startup.

        Deliberately does not raise on partial recovery. An earlier version of the
        equivalent code in `EpisodeService` failed startup here, which is the wrong
        trade: a recoverable backlog would leave the application unusable when it
        could run with reduced memory and a visible backlog on /health.
        """
        records = await self._repository.recoverable(limit)
        if not records:
            return []

        _log.info("recovering_extractions", count=len(records))
        requeued: list[EpisodeId] = []
        for record in records:
            # Bypasses `claim`, which would return False for a row that already
            # exists. Recovery is re-running work the table already owns, not
            # claiming it afresh.
            task = asyncio.create_task(
                self._run(record.episode_id, record.conversation_id),
                name=f"recover-{record.episode_id}",
            )
            self._tasks.add(task)
            if record.conversation_id is not None:
                self._by_conversation[record.conversation_id].add(task)
            task.add_done_callback(self._tasks.discard)
            requeued.append(record.episode_id)

        return requeued

    async def drain(self, timeout: timedelta = timedelta(seconds=30)) -> int:
        """Wait for in-flight extraction to finish. Called at shutdown.

        Without this, `stop()` closes the store while a background task is mid
        transaction, and the shutdown races the write it is trying to complete.
        Returns the number still running when the wait expired.
        """
        outstanding = {task for task in self._tasks if not task.done()}
        if not outstanding:
            return 0

        _log.info("draining_extractions", count=len(outstanding))
        _, pending = await asyncio.wait(outstanding, timeout=timeout.total_seconds())
        if pending:
            _log.warning(
                "extraction_drain_incomplete",
                remaining=len(pending),
                consequence="those episodes stay durable and are retried at next start",
            )
        return len(pending)

    def quiesce(self) -> None:
        """Stop starting new extraction. In-flight work continues (ADR-013)."""
        self._open.clear()

    def resume(self) -> None:
        self._open.set()

    @property
    def in_flight(self) -> int:
        return sum(1 for task in self._tasks if not task.done())

    async def backlog(self) -> dict[str, int]:
        """Counts by state, for /health.

        A stalled coordinator is otherwise invisible: the API keeps returning 200 and
        replies look normal while memory quietly stops accumulating.
        """
        counts = await self._repository.count_by_state()
        counts["in_flight_local"] = self.in_flight
        return counts

    # --------------------------------------------------------------- internals

    async def _run(
        self, episode_id: EpisodeId, conversation_id: ConversationId | None = None
    ) -> None:
        """Execute one extraction. Never raises — failures are recorded, not thrown.

        Nothing awaits this task except the barrier, and the barrier's job is to stop
        waiting rather than to propagate. An exception escaping here would surface as
        an unretrieved-task warning at interpreter shutdown and nowhere useful.
        """
        await self._open.wait()

        async with self._slots:
            await self._repository.mark_running(episode_id, self._clock.now())
            try:
                outcome = await asyncio.wait_for(
                    self._runner(episode_id),
                    timeout=self._extraction_timeout.total_seconds(),
                )
            except TimeoutError:
                # Distinct from FAILED. The work did not fail, it ran long, and it is
                # still owed a retry — so `recoverable()` includes ABANDONED.
                _log.error(
                    "extraction_timed_out",
                    episode_id=str(episode_id),
                    seconds=self._extraction_timeout.total_seconds(),
                    consequence="episode stays durable; retried at next recovery",
                )
                await self._repository.mark_finished(
                    episode_id,
                    ExtractionState.ABANDONED,
                    self._clock.now(),
                    error=f"exceeded {self._extraction_timeout.total_seconds()}s",
                )
                return
            except asyncio.CancelledError:
                await self._repository.mark_finished(
                    episode_id,
                    ExtractionState.ABANDONED,
                    self._clock.now(),
                    error="cancelled during shutdown",
                )
                raise
            except Exception as exc:  # noqa: BLE001 - recorded, never propagated
                _log.error(
                    "extraction_failed",
                    episode_id=str(episode_id),
                    error=str(exc)[:300],
                    consequence="message saved; not searchable until recovery",
                )
                await self._repository.mark_finished(
                    episode_id,
                    ExtractionState.FAILED,
                    self._clock.now(),
                    error=str(exc)[:2000],
                )
                return

        await self._repository.mark_finished(
            episode_id, ExtractionState.SUCCEEDED, self._clock.now()
        )
        if conversation_id is not None:
            self._queue_notices(conversation_id, outcome)
        _log.info(
            "extraction_completed",
            episode_id=str(episode_id),
            facts=outcome.facts_committed,
            needs_clarification=outcome.needs_clarification,
        )

    def _queue_notices(
        self, conversation_id: ConversationId, outcome: ExtractionOutcome
    ) -> None:
        """Hold findings for delivery on the conversation's next turn."""
        if outcome.contradictions:
            self._notices[conversation_id].append(
                "Some of what you said conflicts with what I had recorded: "
                + "; ".join(outcome.contradictions[:3])
                + ". Both versions were kept."
            )
        if outcome.needs_clarification:
            self._notices[conversation_id].append(
                "One or more people mentioned could not be identified "
                "unambiguously. The details were saved separately pending review."
            )

    def take_notices(self, conversation_id: ConversationId) -> list[str]:
        """Drain findings owed to this conversation.

        Draining rather than reading: a notice repeated on every subsequent turn would
        train the user to ignore it, and the condition it describes is reported
        durably by the inspection API rather than by this transient channel.
        """
        return self._notices.pop(conversation_id, [])
