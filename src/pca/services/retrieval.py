"""RetrievalService — hybrid retrieval (FR-06.2 through FR-06.6).

Layer L3.

Five strategies, fused, seeded-traversed, reranked, and governed by an explicit stop
condition. The shape:

    1. four strategies CONCURRENTLY   semantic, full-text, entity-scoped, temporal
    2. RRF fusion + dedup             ranks, not scores (see services/fusion.py)
    3. seeded traversal               from the best fused hits, never blind
    4. cross-encoder rerank           once, over the combined set, capped
    5. governor trims                 to the item and character ceilings
    6. resolve against PostgreSQL     the graph found candidates; Postgres asserts

Step 6 is the one that is easy to skip and must not be. ADR-015 is explicit: the
graph is queried to FIND things, PostgreSQL is queried to ASSERT what is true.
Graphiti maintains its own entity consolidation and edge invalidation, and treating
its edges as facts would give the system two temporal models that disagree — so
"what was true in March?" would answer differently depending on which store replied.

Unit 1b's version returned `RetrievalResult(diagnostics=...)` with NO facts at all;
the assembled context was raw graph text passed through a `raw_hits` side channel.
That is why `raw_hits` is gone: typed, authoritative facts now flow properly.

Strategy failures are isolated. One strategy raising degrades that strategy, not the
request — recorded in `StrategySpend.failed` so a persistently broken strategy is
visible rather than merely quiet. Only a total graph failure sets `degraded`, and
even then retrieval falls back to PostgreSQL (NFR-06.5) rather than returning
nothing.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from datetime import datetime, timedelta

from pca.domain.ids import EntityId
from pca.domain.memory import Entity, Fact, Relationship
from pca.domain.retrieval import (
    RetrievalDiagnostics,
    RetrievalQuery,
    RetrievalResult,
    RetrievalStrategy,
    Spend,
    StrategySpend,
)
from pca.observability.logging import get_logger
from pca.ports.graph import GraphHit, MemoryGraphPort
from pca.ports.repositories import (
    BeliefRepositoryPort,
    EntityRepositoryPort,
    MemoryRepositoryPort,
)
from pca.services.budget import DEFAULT_BUDGET, RetrievalBudgetGovernor
from pca.services.fusion import agreement, fuse

_log = get_logger(__name__)

# How many graph candidates each strategy may return. Larger than the final item
# ceiling on purpose: fusion and reranking need a surplus to choose from, and a
# strategy limited to the output size cannot contribute anything the others missed.
_PER_STRATEGY_LIMIT = 15

_TRAVERSAL_DEPTH = 2
_TRAVERSAL_SEEDS = 3


def _normalise(statement: str) -> str:
    """Key for matching a graph edge's text to a PostgreSQL fact.

    Text matching is the honest seam here and it is imperfect. Graphiti rewrites
    extracted facts in its own words, so its edge text and our `facts.statement` are
    two independent paraphrases of the same utterance and will not always align.

    A shared id would be better, but Graphiti assigns edge ids during its own
    extraction pass with no knowledge of our commit, so no such id exists. Rather
    than pretend the match is reliable, unmatched graph hits are handled explicitly
    by `_resolve_facts` — they contribute ranking signal, never content.
    """
    return " ".join(statement.lower().split())


class RetrievalService:
    """Hybrid retrieval across the graph, resolved against the system of record."""

    def __init__(
        self,
        graph: MemoryGraphPort,
        memory: MemoryRepositoryPort | None = None,
        entities: EntityRepositoryPort | None = None,
        beliefs: BeliefRepositoryPort | None = None,
        governor: RetrievalBudgetGovernor | None = None,
    ) -> None:
        self._graph = graph
        self._memory = memory
        self._entities = entities
        self._beliefs = beliefs
        self._governor = governor or RetrievalBudgetGovernor()

    def budget_for(self, intent: str | None = None):  # type: ignore[no-untyped-def]
        """The budget for a request. Delegates to the governor.

        Exposed here so callers do not need the governor injected separately just to
        build a query — the workflow needs a budget, not a policy object.
        """
        return self._governor.budget_for(intent)

    async def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        started = time.perf_counter()
        spends: list[StrategySpend] = []
        notes: list[str] = []

        per_strategy = await self._run_strategies(query, spends)

        # ANY failed strategy means degraded, not all of them. Each strategy exists
        # to catch what the others miss, so losing one genuinely means older context
        # may be absent — and NFR-06.5 requires that be disclosed rather than
        # answered over. Requiring all to fail was wrong: with the graph down but one
        # strategy happening to return an empty list without raising, the system
        # reported healthy retrieval over missing memory, which is the precise
        # failure this product cannot afford.
        any_failed = any(s.failed for s in spends)
        all_failed = bool(spends) and all(s.failed for s in spends)

        fused = fuse(per_strategy)
        found_by = agreement(per_strategy)

        spent = Spend(
            elapsed=timedelta(seconds=time.perf_counter() - started),
            chars=sum(len(h.content) for h in fused),
        )

        stopped_early = False
        stop_reason: str | None = None

        # Traversal is a second round trip, so it is the first thing the governor
        # refuses. Skipping it costs breadth; overrunning the budget costs the whole
        # response (NFR-02.1).
        if self._governor.should_continue(spent, len(fused)):
            traversed = await self._traverse_from(query.text, fused, spends)
            if traversed:
                fused = fuse(
                    [
                        ("fused", fused),
                        (RetrievalStrategy.TRAVERSAL.value, traversed),
                    ]
                )
                found_by = {**found_by, **agreement([(RetrievalStrategy.TRAVERSAL.value, traversed)])}
        else:
            stopped_early = True
            stop_reason = self._governor.stop_reason(spent, len(fused))
            notes.append(f"traversal skipped — {stop_reason}")

        fused_count = len(fused)

        reranked = await self._rerank(query.text, fused, spends)

        kept, dropped = self._governor.trim(reranked, lambda h: len(h.content))

        facts, entities, relationships = await self._resolve(query, kept, notes)

        diagnostics = RetrievalDiagnostics(
            spends=spends,
            fused_count=fused_count,
            reranked_count=len(reranked),
            dropped_by_budget=dropped,
            degraded=any_failed,
            notes=notes + self._degradation_notes(all_failed, spends),
            stopped_early=stopped_early,
            stop_reason=stop_reason,
        )

        _log.info(
            "retrieval_complete",
            contributing=diagnostics.contributing_strategies,
            failed=diagnostics.failed_strategies,
            fused=fused_count,
            kept=len(kept),
            dropped=dropped,
            facts=len(facts),
            degraded=any_failed,
            stopped_early=stopped_early,
            ms=round(diagnostics.total_duration.total_seconds() * 1000),
            multi_strategy_hits=sum(1 for refs in found_by.values() if len(refs) > 1),
        )

        return RetrievalResult(
            facts=facts,
            entities=entities,
            relationships=relationships,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _degradation_notes(
        all_failed: bool, spends: Sequence[StrategySpend]
    ) -> list[str]:
        """Describe what was lost, distinguishing a total outage from a partial one.

        A total graph outage and one broken strategy both warrant disclosure, but
        they are different operational facts and collapsing them would make the logs
        useless for deciding whether to page anyone.
        """
        failed = [s.strategy for s in spends if s.failed]
        if not failed:
            return []
        if all_failed:
            return ["graph unavailable; answered from PostgreSQL only"]
        return [f"retrieval strategies unavailable: {', '.join(failed)}"]

    # --------------------------------------------------------------- strategies

    async def _run_strategies(
        self, query: RetrievalQuery, spends: list[StrategySpend]
    ) -> list[tuple[str, Sequence[GraphHit]]]:
        """Run the four text/scope strategies concurrently.

        Concurrent because they are independent and the budget is wall-clock: run
        serially, four strategies at ~1s each consume four times the latency for the
        same information. `return_exceptions=True` so one failure cannot cancel the
        siblings.
        """
        names = await self._scope_names(query)
        window = self._window(query)

        planned: list[tuple[str, object]] = [
            (RetrievalStrategy.SEMANTIC.value, self._graph.search_semantic(query.text, _PER_STRATEGY_LIMIT)),
            (RetrievalStrategy.FULLTEXT.value, self._graph.search_fulltext(query.text, _PER_STRATEGY_LIMIT)),
        ]
        if names:
            planned.append(
                (
                    RetrievalStrategy.ENTITY.value,
                    self._entity_scoped(names),
                )
            )
        if window is not None:
            planned.append(
                (
                    RetrievalStrategy.TEMPORAL.value,
                    self._graph.search_temporal(query.text, window, _PER_STRATEGY_LIMIT),
                )
            )

        started = time.perf_counter()
        outcomes = await asyncio.gather(
            *(coro for _, coro in planned), return_exceptions=True
        )
        elapsed = timedelta(seconds=time.perf_counter() - started)

        results: list[tuple[str, Sequence[GraphHit]]] = []
        for (name, _), outcome in zip((p for p in planned), outcomes, strict=True):
            if isinstance(outcome, BaseException):
                spends.append(
                    StrategySpend(strategy=name, duration=elapsed, hits=0, failed=True)
                )
                _log.warning(
                    "retrieval_strategy_failed",
                    strategy=name,
                    error=str(outcome)[:200],
                    consequence="other strategies continue; contribution recorded as failed",
                )
                continue
            hits = list(outcome)  # type: ignore[arg-type]
            # Duration is the wall-clock of the whole concurrent batch, attributed to
            # each strategy. Per-strategy timing would need separate measurement
            # inside each coroutine; the batch figure is what actually bounds the
            # request, which is what the budget cares about.
            spends.append(
                StrategySpend(strategy=name, duration=elapsed, hits=len(hits))
            )
            results.append((name, hits))

        return results

    async def _entity_scoped(self, names: Sequence[str]) -> list[GraphHit]:
        """Entity-scoped search across every named entity in scope."""
        gathered: list[GraphHit] = []
        for name in names:
            gathered.extend(
                await self._graph.search_by_entity(name, _PER_STRATEGY_LIMIT)
            )
        return gathered

    async def _traverse_from(
        self, query_text: str, fused: Sequence[GraphHit], spends: list[StrategySpend]
    ) -> list[GraphHit]:
        """Expand from the best fused hits.

        Seeds are graph node ids taken from the top results — never a blind sweep.
        Only the top few, because traversal fans out fast and a mediocre seed
        contributes noise proportional to its degree (FR-06.5).
        """
        seeds: list[str] = []
        for hit in fused[:_TRAVERSAL_SEEDS]:
            node = str(hit.raw.get("source_node_uuid") or "")
            if node and node not in seeds:
                seeds.append(node)

        if not seeds:
            return []

        started = time.perf_counter()
        gathered: list[GraphHit] = []
        failed = False
        for seed in seeds:
            try:
                gathered.extend(
                    await self._graph.traverse(query_text, seed, _TRAVERSAL_DEPTH)
                )
            except Exception as exc:  # noqa: BLE001
                failed = True
                _log.warning("traversal_failed", seed=seed, error=str(exc)[:200])

        spends.append(
            StrategySpend(
                strategy=RetrievalStrategy.TRAVERSAL.value,
                duration=timedelta(seconds=time.perf_counter() - started),
                hits=len(gathered),
                failed=failed and not gathered,
            )
        )
        return gathered

    async def _rerank(
        self, text: str, fused: list[GraphHit], spends: list[StrategySpend]
    ) -> list[GraphHit]:
        """Cross-encode the fused set, capped.

        The cap is not optional: the Gemini reranker costs one API call per passage.
        Anything beyond the cutoff keeps its fused rank and is appended after the
        reranked head, so nothing is silently discarded here — trimming is the
        governor's job, not the reranker's.
        """
        if len(fused) <= 1:
            return fused

        cutoff = self._governor.rerank_cutoff(len(fused))
        head, tail = fused[:cutoff], fused[cutoff:]

        started = time.perf_counter()
        try:
            ranked = await self._graph.rerank(text, head)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "rerank_unavailable",
                error=str(exc)[:200],
                consequence="fused order retained",
            )
            return fused

        if tail:
            _log.info("rerank_capped", reranked=len(head), appended_unranked=len(tail))
        return list(ranked) + tail

    # ------------------------------------------------------------- resolution

    async def _scope_names(self, query: RetrievalQuery) -> list[str]:
        """Entity names for the ids in scope.

        The graph is searched by name because Graphiti's node ids are assigned by its
        own extraction pass and are not our EntityIds. Resolution lives here rather
        than in the adapter so the adapter needs no repository.
        """
        if not query.entity_scope or self._entities is None:
            return []

        names: list[str] = []
        for entity_id in query.entity_scope:
            entity = await self._entities.get(entity_id)
            if entity is not None and entity.name not in names:
                names.append(entity.name)
        return names

    @staticmethod
    def _window(query: RetrievalQuery) -> tuple[datetime, datetime] | None:
        """A closed window, or None when the query is not time-scoped.

        Temporal search only runs when the caller actually asked about a period.
        Defaulting to "all time" would make this strategy a duplicate of semantic
        search with extra latency.
        """
        if query.time_range is None:
            return None
        start, end = query.time_range
        if start is None or end is None:
            return None
        return (start, end)

    async def _resolve(
        self,
        query: RetrievalQuery,
        hits: Sequence[GraphHit],
        notes: list[str],
    ) -> tuple[list[Fact], list[Entity], list[Relationship]]:
        """Turn graph candidates into authoritative records (ADR-015).

        Without a memory repository this returns nothing rather than fabricating
        facts from graph text — which is what the Unit 1b `raw_hits` channel
        effectively did, presenting Graphiti's paraphrase as remembered fact.
        """
        if self._memory is None:
            return [], [], []

        facts = await self._resolve_facts(query, hits, notes)
        entities = await self._resolve_entities(query)
        relationships = await self._resolve_relationships(query)
        return facts, entities, relationships

    async def _resolve_facts(
        self,
        query: RetrievalQuery,
        hits: Sequence[GraphHit],
        notes: list[str],
    ) -> list[Fact]:
        """Load authoritative facts, ordered by the graph's ranking where possible.

        Two sources, deliberately:

        - entity scope, straight from PostgreSQL. Precise and not dependent on the
          graph having extracted anything.
        - the active fact set, matched against graph hit text to inherit rank.

        Facts the graph did not surface are still included, after the ranked ones.
        Excluding them would make retrieval quality entirely hostage to Graphiti's
        extraction, and a fact we committed but it never indexed would become
        permanently unreachable — the failure mode ADR-015 exists to prevent.
        """
        assert self._memory is not None

        candidates: dict[str, Fact] = {}
        order: list[Fact] = []

        if query.entity_scope:
            for entity_id in query.entity_scope:
                for fact in await self._memory.facts_for_entity(
                    entity_id, limit=self._governor.budget.max_items
                ):
                    candidates.setdefault(_normalise(fact.statement), fact)

        for fact in await self._memory.active_facts(limit=100):
            candidates.setdefault(_normalise(fact.statement), fact)

        matched: set[str] = set()
        for hit in hits:
            key = _normalise(hit.content)
            fact = candidates.get(key)
            if fact is not None and key not in matched:
                matched.add(key)
                order.append(fact)

        unmatched_hits = len(hits) - len(matched)
        if unmatched_hits > 0:
            # Expected, not alarming: Graphiti paraphrases. Recorded because a ratio
            # near 100% would mean the text-matching seam has broken entirely and
            # ranking is doing nothing.
            notes.append(
                f"{unmatched_hits} of {len(hits)} graph hits did not match a stored "
                "fact statement (Graphiti paraphrases; ranking signal only)"
            )

        # Salience-ordered remainder (ADR-017): aggressive extraction means trivia
        # accumulates, and the failure mode is burying signal rather than losing it.
        remainder = sorted(
            (f for key, f in candidates.items() if key not in matched),
            key=lambda f: f.salience,
            reverse=True,
        )
        combined = order + remainder

        kept, _ = self._governor.trim(combined, lambda f: len(f.statement))
        return kept

    async def _resolve_entities(self, query: RetrievalQuery) -> list[Entity]:
        if self._entities is None or not query.entity_scope:
            return []
        found: list[Entity] = []
        for entity_id in query.entity_scope:
            entity = await self._entities.get(entity_id)
            if entity is not None:
                found.append(entity)
        return found

    async def _resolve_relationships(
        self, query: RetrievalQuery
    ) -> list[Relationship]:
        if self._memory is None or not query.entity_scope:
            return []
        found: list[Relationship] = []
        seen: set[EntityId] = set()
        for entity_id in query.entity_scope:
            if entity_id in seen:
                continue
            seen.add(entity_id)
            found.extend(await self._memory.relationships_for_entity(entity_id))
        return found


__all__ = ["DEFAULT_BUDGET", "RetrievalService"]
