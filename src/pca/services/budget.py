"""RetrievalBudgetGovernor — when to stop searching (FR-06.3, FR-06.5, NFR-02.1).

Layer L3. Not async: pure policy, no I/O.

It exists as its own class rather than as a few `if` statements inside
`RetrievalService` because the stop condition is the part of retrieval most likely
to need tuning, and tuning something buried in an async orchestration method means
re-running the whole pipeline to test a threshold.

The distinction it enforces, from `RetrievalBudget`'s own docstring:

    A fixed `limit` is not a stop condition; it is a truncation.

Truncation happens after the work is done and the latency is already spent. A stop
condition prevents the next expensive call. Both are needed, and this class does
both — `should_continue` before each stage, `trim` at the end — but they are
separate methods because they answer different questions.

`max_items` deliberately does NOT gate `should_continue`. Gathering more candidates
than we will keep is the point: fusion and reranking need a surplus to choose from,
and stopping at exactly `max_items` would hand the reranker a set it cannot improve.
"""

from __future__ import annotations

from datetime import timedelta

from pca.domain.retrieval import RetrievalBudget, Spend
from pca.observability.logging import get_logger

_log = get_logger(__name__)

DEFAULT_BUDGET = RetrievalBudget(
    max_duration=timedelta(seconds=25),
    max_items=12,
    max_context_chars=8_000,
)

# Fraction of the duration budget that may be spent before refusing to start
# another stage. Below 1.0 because a stage that starts at 99% of budget will still
# overrun it — the check has to leave room for the work it is authorising.
_DURATION_HEADROOM = 0.75

# Hard ceiling on how many candidates reach the cross-encoder. GeminiRerankerClient
# issues ONE API CALL PER PASSAGE, so reranking 60 fused hits means 60 Gemini calls
# and a latency blowout that no amount of concurrency hides. This cap is the
# difference between reranking being a refinement and being the dominant cost.
MAX_RERANK_CANDIDATES = 20


class RetrievalBudgetGovernor:
    """Decides when retrieval has done enough."""

    def __init__(self, budget: RetrievalBudget = DEFAULT_BUDGET) -> None:
        self._budget = budget

    @property
    def budget(self) -> RetrievalBudget:
        return self._budget

    def budget_for(self, intent: str | None = None) -> RetrievalBudget:
        """The budget for a request.

        `intent` is accepted and currently ignored. The approved design signature is
        `budget_for(intent: Intent)`, but `Intent` is introduced by Unit 5's
        IntentRouter and does not exist yet. Taking a permissive `str | None` keeps
        the call site stable so Unit 5 can add routing without changing every caller,
        rather than inventing a placeholder Intent type this unit has no use for.
        """
        return self._budget

    def should_continue(self, spent: Spend, gathered: int) -> bool:
        """Whether to run another retrieval stage.

        Checks time and context size, not item count — see the module docstring on
        why `max_items` must not gate this.
        """
        if spent.elapsed >= self._budget.max_duration * _DURATION_HEADROOM:
            return False
        return spent.chars < self._budget.max_context_chars

    def stop_reason(self, spent: Spend, gathered: int) -> str | None:
        """Why `should_continue` returned False. None when it would return True.

        Separate from the decision so the decision stays a cheap boolean while the
        explanation is still available for diagnostics. A governor that stops
        without saying why is untunable.
        """
        if spent.elapsed >= self._budget.max_duration * _DURATION_HEADROOM:
            return (
                f"duration budget: spent {spent.elapsed.total_seconds():.1f}s of "
                f"{self._budget.max_duration.total_seconds():.0f}s allowance"
            )
        if spent.chars >= self._budget.max_context_chars:
            return (
                f"context ceiling: {spent.chars} chars reached "
                f"{self._budget.max_context_chars} limit"
            )
        return None

    def rerank_cutoff(self, candidate_count: int) -> int:
        """How many candidates may reach the cross-encoder.

        Capped because the Gemini reranker costs one API call per passage.
        """
        return min(candidate_count, MAX_RERANK_CANDIDATES)

    def trim(self, items: list, chars_of) -> tuple[list, int]:  # type: ignore[type-arg]
        """Cut a ranked list to fit both ceilings. Returns (kept, dropped_count).

        Applied to an ALREADY RANKED list, so trimming removes the least relevant
        items rather than an arbitrary tail. Calling this on unranked input would
        discard good context and keep noise.

        `chars_of` measures one item's contribution to the context. Passed in rather
        than assumed, because this governor should not know whether it is trimming
        facts, hits, or rendered strings.
        """
        kept: list = []  # type: ignore[type-arg]
        chars = 0

        for item in items:
            if len(kept) >= self._budget.max_items:
                break
            size = chars_of(item)
            if chars + size > self._budget.max_context_chars and kept:
                # `and kept` so a single oversized item still yields something
                # rather than returning an empty context and looking like a
                # retrieval failure.
                break
            kept.append(item)
            chars += size

        dropped = len(items) - len(kept)
        if dropped:
            _log.info(
                "budget_trimmed",
                kept=len(kept),
                dropped=dropped,
                chars=chars,
                max_items=self._budget.max_items,
                max_chars=self._budget.max_context_chars,
            )
        return kept, dropped
