"""RetrievalService — naive version for Unit 1b.

Layer L3.

Scope: semantic search only, with a hard item cap. This is knowingly a placeholder.

Unit 4 replaces it with the real thing: five strategies running concurrently
(semantic, full-text, entity-scoped, temporal, graph traversal), fused, then
seeded traversal, then cross-encoder reranking, all governed by an explicit stop
condition rather than a fixed limit (FR-06.2 through FR-06.6).

Diagnostics are populated even in this naive form. A 25-second budget cannot be
tuned blind, and emitting them from the start means Unit 4 has a baseline to
compare against rather than starting from nothing.
"""

from __future__ import annotations

import time
from datetime import timedelta

from pca.domain.errors import MemoryGraphUnavailable
from pca.domain.retrieval import (
    RetrievalBudget,
    RetrievalDiagnostics,
    RetrievalQuery,
    RetrievalResult,
    StrategySpend,
)
from pca.observability.logging import get_logger
from pca.ports.graph import GraphHit, MemoryGraphPort

_log = get_logger(__name__)

DEFAULT_BUDGET = RetrievalBudget(
    max_duration=timedelta(seconds=25),
    max_items=12,
    max_context_chars=8_000,
)


class RetrievalService:
    """Naive semantic-only retrieval."""

    def __init__(self, graph: MemoryGraphPort) -> None:
        self._graph = graph

    async def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        """Fetch candidate context.

        Degrades rather than fails when the graph is unavailable (NFR-06.5). The
        conversation can still proceed on its own history, and the degradation is
        recorded in diagnostics so the caller can disclose it — an answer built on
        missing history must not look confident.
        """
        started = time.perf_counter()
        hits: list[GraphHit] = []
        diagnostics_notes: list[str] = []
        degraded = False

        try:
            hits = await self._graph.search_semantic(query.text, limit=query.budget.max_items)
        except Exception as exc:  # noqa: BLE001 - degradation is the point
            degraded = True
            diagnostics_notes.append(f"semantic search unavailable: {type(exc).__name__}")
            _log.warning("retrieval_degraded", error=str(exc)[:200])

        elapsed = timedelta(seconds=time.perf_counter() - started)

        dropped = 0
        if len(hits) > query.budget.max_items:
            dropped = len(hits) - query.budget.max_items
            hits = hits[: query.budget.max_items]

        diagnostics = RetrievalDiagnostics(
            spends=[
                StrategySpend(strategy="semantic", duration=elapsed, hits=len(hits))
            ],
            fused_count=len(hits),
            reranked_count=0,
            dropped_by_budget=dropped,
            degraded=degraded,
            notes=diagnostics_notes
            + ["naive retrieval: semantic only, no fusion or reranking (Unit 4 replaces this)"],
        )

        _log.info(
            "retrieval_complete",
            hits=len(hits),
            degraded=degraded,
            ms=round(elapsed.total_seconds() * 1000),
        )

        # Naive form returns raw graph hits as source context. Unit 3 populates
        # typed Facts and Events once the memory write path exists.
        return RetrievalResult(diagnostics=diagnostics)

    async def raw_hits(self, text: str, limit: int = 12) -> list[GraphHit]:
        """Direct access to graph hits.

        Exists because the naive ContextAssemblyService has no typed memory to
        work with yet and must render something. Removed in Unit 4.
        """
        try:
            return await self._graph.search_semantic(text, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise MemoryGraphUnavailable(str(exc)) from exc
