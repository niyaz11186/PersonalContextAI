"""Inline extraction coordinator for integration tests.

Unit 5 moved extraction off the request path, which is the point of ADR-008 — and it
makes any test that sends a message and then asserts on committed memory a race. The
HTTP response returns before the background task has necessarily run.

Two ways to deal with that, and both are needed for different reasons:

  * **This double**, which awaits the runner inside `submit`. Integration tests keep
    asserting on end state without sleeping or draining, and they stay fast and
    deterministic. What they lose is any coverage of the asynchrony itself.
  * **The real `ExtractionCoordinator`**, exercised directly in
    `tests/unit/test_extraction_coordinator.py` — per-conversation isolation, barrier
    timeout, duplicate submit, crash recovery, bounded concurrency.

Splitting it this way is deliberate. Trying to test barrier semantics through
`TestClient` would mean sleeping on wall-clock time and hoping, which produces tests
that pass on a fast machine and flake on a loaded one. The scheduling belongs in unit
tests where time is controllable; the integration tests care that the pipeline runs at
all and that its results reach the database.

`await_barrier` always reports clear, because with inline extraction there is by
construction nothing outstanding by the time it is called.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import timedelta

from pca.domain.ids import ConversationId, EpisodeId
from pca.domain.orchestration import BarrierResult, ExtractionOutcome

ExtractionRunner = Callable[[EpisodeId], Awaitable[ExtractionOutcome]]


class InlineExtractionCoordinator:
    """Runs extraction immediately, in the caller's task."""

    def __init__(self, runner: ExtractionRunner) -> None:
        self._runner = runner
        self.submitted: list[EpisodeId] = []
        self.outcomes: list[ExtractionOutcome] = []
        self.errors: list[Exception] = []
        self.barrier_calls: list[ConversationId] = []
        self.notices: dict[ConversationId, list[str]] = defaultdict(list)
        self._open = True

    async def submit(
        self, episode_id: EpisodeId, conversation_id: ConversationId | None
    ) -> bool:
        self.submitted.append(episode_id)
        try:
            outcome = await self._runner(episode_id)
        except Exception as exc:  # noqa: BLE001
            # The real coordinator records failures rather than raising, because
            # nothing awaits its tasks. Mirrored here so a broken extraction surfaces
            # the same way in tests as in production: as a recorded failure, not as an
            # exception escaping the request.
            self.errors.append(exc)
            return True

        self.outcomes.append(outcome)
        if conversation_id is not None:
            self._queue_notices(conversation_id, outcome)
        return True

    def _queue_notices(
        self, conversation_id: ConversationId, outcome: ExtractionOutcome
    ) -> None:
        """Same wording as the real coordinator, for the same reason.

        Still delivered one turn late, even though extraction is inline here: the API
        drains notices *before* submitting, so anything queued during this turn is read
        on the next one. That ordering is deliberate — it means integration tests
        observe the same one-turn delay real users will, rather than a convenience the
        double invented.
        """
        if outcome.contradictions:
            self.notices[conversation_id].append(
                "Some of what you said conflicts with what I had recorded: "
                + "; ".join(outcome.contradictions[:3])
                + ". Both versions were kept."
            )
        if outcome.needs_clarification:
            self.notices[conversation_id].append(
                "One or more people mentioned could not be identified "
                "unambiguously. The details were saved separately pending review."
            )

    def take_notices(self, conversation_id: ConversationId) -> list[str]:
        return self.notices.pop(conversation_id, [])

    async def await_barrier(
        self, conversation_id: ConversationId, timeout: timedelta | None = None
    ) -> BarrierResult:
        self.barrier_calls.append(conversation_id)
        return BarrierResult(cleared=True, waited=timedelta(0))

    async def recover_pending(self, limit: int = 100) -> list[EpisodeId]:
        return []

    async def drain(self, timeout: timedelta = timedelta(seconds=30)) -> int:
        return 0

    def quiesce(self) -> None:
        self._open = False

    def resume(self) -> None:
        self._open = True

    @property
    def in_flight(self) -> int:
        return 0

    async def backlog(self) -> dict[str, int]:
        return {
            "succeeded": len(self.outcomes),
            "failed": len(self.errors),
            "pending": 0,
            "running": 0,
            "in_flight_local": 0,
        }


class DeferredExtractionCoordinator:
    """Defers extraction until the barrier runs it, modelling ADR-008's real ordering.

    `InlineExtractionCoordinator` above runs extraction during `submit`, which is
    convenient but makes the NFR-02.3 assertion vacuous: if extraction has already
    finished by the time the response is built, "the reply did not wait for it" cannot
    be distinguished from "it was fast".

    This double separates the two moments the way the real coordinator does:

        submit()         records the work and returns immediately. Nothing runs.
        await_barrier()  runs whatever that conversation still owes, then clears.

    Which makes two properties observable through HTTP:

      * the SSE `done` event arrives with the episode's facts NOT yet committed —
        the assertion that actually retires the Unit 1b synchronous-extraction
        exception, and one that would fail against the old code
      * the next message in that conversation sees those facts, because the barrier
        settled them first — the core product hypothesis

    It does not model *concurrency*: nothing here runs in the background, so timeouts,
    bounded parallelism, and per-conversation isolation are not exercised. Those are
    covered against the real `ExtractionCoordinator` in
    `tests/unit/test_extraction_coordinator.py`, where time is controllable. Asserting
    them through `TestClient` would mean sleeping on wall clock and hoping.
    """

    def __init__(self, runner: ExtractionRunner) -> None:
        self._runner = runner
        self._pending: dict[ConversationId | None, list[EpisodeId]] = defaultdict(list)
        self.submitted: list[EpisodeId] = []
        self.ran: list[EpisodeId] = []
        self.outcomes: list[ExtractionOutcome] = []
        self.errors: list[Exception] = []
        self.notices: dict[ConversationId, list[str]] = defaultdict(list)
        self.barrier_calls: list[ConversationId] = []

    async def submit(
        self, episode_id: EpisodeId, conversation_id: ConversationId | None
    ) -> bool:
        self.submitted.append(episode_id)
        self._pending[conversation_id].append(episode_id)
        return True

    async def await_barrier(
        self, conversation_id: ConversationId, timeout: timedelta | None = None
    ) -> BarrierResult:
        self.barrier_calls.append(conversation_id)
        owed = self._pending.pop(conversation_id, [])
        for episode_id in owed:
            await self._settle(episode_id, conversation_id)
        return BarrierResult(cleared=True, waited=timedelta(0))

    async def run_pending(self) -> int:
        """Settle everything, regardless of conversation. For explicit test control."""
        count = 0
        for conversation_id in list(self._pending):
            for episode_id in self._pending.pop(conversation_id, []):
                await self._settle(episode_id, conversation_id)
                count += 1
        return count

    @property
    def pending_count(self) -> int:
        return sum(len(v) for v in self._pending.values())

    async def _settle(
        self, episode_id: EpisodeId, conversation_id: ConversationId | None
    ) -> None:
        self.ran.append(episode_id)
        try:
            outcome = await self._runner(episode_id)
        except Exception as exc:  # noqa: BLE001 - recorded, never propagated
            self.errors.append(exc)
            return

        self.outcomes.append(outcome)
        if conversation_id is None:
            return
        if outcome.contradictions:
            self.notices[conversation_id].append(
                "Some of what you said conflicts with what I had recorded: "
                + "; ".join(outcome.contradictions[:3])
                + ". Both versions were kept."
            )
        if outcome.needs_clarification:
            self.notices[conversation_id].append(
                "One or more people mentioned could not be identified "
                "unambiguously. The details were saved separately pending review."
            )

    def take_notices(self, conversation_id: ConversationId) -> list[str]:
        return self.notices.pop(conversation_id, [])

    async def recover_pending(self, limit: int = 100) -> list[EpisodeId]:
        return []

    async def drain(self, timeout: timedelta = timedelta(seconds=30)) -> int:
        return self.pending_count

    def quiesce(self) -> None:
        pass

    def resume(self) -> None:
        pass

    @property
    def in_flight(self) -> int:
        return 0

    async def backlog(self) -> dict[str, int]:
        return {
            "succeeded": len(self.outcomes),
            "failed": len(self.errors),
            "pending": self.pending_count,
            "running": 0,
            "in_flight_local": 0,
        }
