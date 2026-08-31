"""MemoryGraphPort — the temporal knowledge graph.

Layer L4.

This port exists to confine Graphiti. The framework-immaturity entry in the risk
register is only survivable if `graphiti_core` never appears outside its adapter
(boundary rule 1), so nothing in these signatures uses a Graphiti type.

ADR-015 governs how results here are treated: Graphiti performs its own entity
consolidation and temporal edge invalidation, and those are **retrieval
optimisations, not truth**. The graph is queried to *find* candidates;
PostgreSQL is queried to *assert* what is true. Without that rule the system
would hold two temporal models that can disagree, and "what was true in March?"
would answer differently depending on which store replied.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from pca.domain.conversation import Episode
from pca.domain.ids import EntityId


@dataclass(frozen=True, slots=True)
class GraphHit:
    """A candidate from the graph. Deliberately loose — this is a search result,
    not an assertion of truth."""

    ref: str
    content: str
    score: float
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    entity_ids: list[EntityId] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GraphIngestResult:
    episode_ref: str
    entities_touched: int = 0
    edges_touched: int = 0


@dataclass(frozen=True, slots=True)
class EntityDivergence:
    """Where Graphiti's internal consolidation disagrees with our authoritative
    entity records (ADR-015). Consumed by ReindexService.verify."""

    graph_ref: str
    our_entity_id: EntityId | None
    detail: str


class MemoryGraphPort(Protocol):
    async def add_episode(self, episode: Episode) -> GraphIngestResult: ...

    # --- retrieval strategies, fused by RetrievalService (FR-06.2) ---
    async def search_semantic(self, text: str, limit: int) -> list[GraphHit]: ...

    async def search_fulltext(self, text: str, limit: int) -> list[GraphHit]: ...

    async def search_by_entity(self, entity_name: str, limit: int) -> list[GraphHit]:
        """Facts scoped to one entity, identified by NAME rather than by EntityId.

        This takes a name because our `EntityId` and Graphiti's internal node uuid
        are assigned by two independent extraction passes over the same text. They
        never coincide. An earlier version passed `str(entity_id)` as Graphiti's
        `center_node_uuid`, which matched no node and silently returned unscoped
        results — the strategy appeared to work while contributing nothing.

        The caller resolves id to name (EntityService owns that mapping). Pushing
        resolution outward keeps this adapter free of repository dependencies.
        """
        ...

    async def search_temporal(
        self, text: str, window: tuple[datetime, datetime], limit: int
    ) -> list[GraphHit]:
        """Facts relevant to `text` whose validity overlaps the window.

        `text` is required, not optional, even though the strategy's defining
        behaviour is the date filter. Graphiti's own `search()` short-circuits to an
        empty result for ANY empty query string, before it even looks at the search
        config — an adapter that passed `""` to "just filter by date" would silently
        return nothing forever, indistinguishable from an empty graph. This is not
        a workaround for a quirk: filtering to text relevant to the query, within
        the window, is also the more useful retrieval behaviour than "everything
        valid during this window" regardless of relevance.

        Filters on the graph's own `valid_at` / `invalid_at` edge attributes. Those
        are Graphiti's temporal model, not ours (ADR-015), so results are candidates
        to be confirmed against PostgreSQL — never the answer to "what was true".
        """
        ...

    async def traverse(
        self, text: str, seed_ref: str, depth: int, edge_types: list[str] | None = None
    ) -> list[GraphHit]:
        """Expand from a seed node, identified by its GRAPH ref.

        `text` is required for the same reason as `search_temporal`: Graphiti's
        `search()` returns nothing for an empty query string regardless of config,
        even though breadth-first traversal itself never reads the query text. The
        adapter must pass something non-empty purely to get past that gate.

        Takes a graph ref rather than an `EntityId` for the same reason as
        `search_by_entity`. Seeds come from `GraphHit.raw["source_node_uuid"]` on
        already-fused results, which are genuinely Graphiti's own node ids.

        Seeded from already-fused results, never run blind — traversing from a
        bad seed is how irrelevant context floods the package (FR-06.5).
        """
        ...

    async def rerank(self, query: str, hits: list[GraphHit]) -> list[GraphHit]:
        """Cross-encode. Runs after fusion, before assembly; reranking a single
        strategy's output wastes the reranker."""
        ...

    async def invalidate_edge(self, ref: str, at: datetime) -> None: ...

    async def entity_divergence(self) -> list[EntityDivergence]: ...

    async def clear_all(self) -> None:
        """Wipe the graph.

        Only safe because PostgreSQL is the system of record (ADR-005) and the
        graph is rebuildable by replaying episodes. In any other architecture
        this method would be data loss.
        """
        ...

    async def health(self) -> bool: ...
