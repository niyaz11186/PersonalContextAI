"""In-memory fake of MemoryGraphPort.

Substitutes for Graphiti and Neo4j so that services depending on the graph are
testable with no container runtime — which is the whole reason Unit 1a can proceed
while Unit 1b is blocked.

Search here is naive matching, not a simulation of Graphiti's hybrid retrieval. It
exists to let callers be exercised, not to validate retrieval quality; that needs the
real thing and belongs to live verification.

One thing it DOES model faithfully, because Unit 4 depends on it: semantic and
full-text search return different things. `search_semantic` matches loosely on any
shared word, `search_fulltext` requires the whole phrase. If both were the same
substring match, a test asserting "strategies contribute independently" would pass
against an implementation that ran one strategy twice.

`entity_names` maps a hit to the entity names it concerns, because the real port
searches the graph BY NAME — our EntityId is not Graphiti's node id.
"""

from datetime import datetime

from pca.domain.conversation import Episode
from pca.ports.graph import EntityDivergence, GraphHit, GraphIngestResult


class FakeMemoryGraph:
    """Dict-backed implementation of MemoryGraphPort."""

    def __init__(self, healthy: bool = True) -> None:
        self.episodes: list[Episode] = []
        self.hits: list[GraphHit] = []
        self.invalidated: list[tuple[str, datetime]] = []
        self.cleared = False
        self._healthy = healthy

        # name -> refs of hits concerning that entity. Populated by tests.
        self.entity_names: dict[str, list[str]] = {}
        # seed node ref -> hits reachable from it.
        self.adjacency: dict[str, list[GraphHit]] = {}

        self.calls: list[str] = []
        self.fail_strategies: set[str] = set()
        self.reranked_with: list[tuple[str, int]] = []

    # ------------------------------------------------------------- test setup

    def add_hit(
        self,
        ref: str,
        content: str,
        score: float = 0.5,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        entity_names: list[str] | None = None,
        source_node: str | None = None,
    ) -> GraphHit:
        """Register a hit and its entity/graph linkage in one call."""
        hit = GraphHit(
            ref=ref,
            content=content,
            score=score,
            valid_from=valid_from,
            valid_to=valid_to,
            raw={"source_node_uuid": source_node or f"node:{ref}"},
        )
        self.hits.append(hit)
        for name in entity_names or []:
            self.entity_names.setdefault(name, []).append(ref)
        return hit

    def _guard(self, strategy: str) -> None:
        self.calls.append(strategy)
        if strategy in self.fail_strategies:
            raise RuntimeError(f"{strategy} unavailable")

    # ---------------------------------------------------------------- writes

    async def add_episode(self, episode: Episode) -> GraphIngestResult:
        # Guarded like the search strategies so `fail_strategies={"add_episode"}`
        # can exercise the ADR-005 path where the graph is down but PostgreSQL is
        # authoritative and the commit must still proceed.
        self._guard("add_episode")
        self.episodes.append(episode)
        self.hits.append(
            GraphHit(
                ref=f"episode:{episode.id}",
                content=episode.content,
                score=1.0,
                valid_from=episode.occurred_at,
                raw={"source_node_uuid": f"node:episode:{episode.id}"},
            )
        )
        return GraphIngestResult(episode_ref=f"episode:{episode.id}")

    # ---------------------------------------------------------------- search

    async def search_semantic(self, text: str, limit: int) -> list[GraphHit]:
        """Loose: any shared word counts. Stands in for embedding similarity."""
        self._guard("semantic")
        words = {w for w in text.lower().split() if len(w) > 2}
        found = [
            h
            for h in self.hits
            if words & {w for w in h.content.lower().split() if len(w) > 2}
        ]
        return found[:limit]

    async def search_fulltext(self, text: str, limit: int) -> list[GraphHit]:
        """Strict: the whole phrase must appear. Stands in for BM25 on exact terms."""
        self._guard("fulltext")
        needle = text.lower().strip()
        return [h for h in self.hits if needle in h.content.lower()][:limit]

    async def search_by_entity(self, entity_name: str, limit: int) -> list[GraphHit]:
        self._guard("entity")
        refs = self.entity_names.get(entity_name, [])
        return [h for h in self.hits if h.ref in refs][:limit]

    async def search_temporal(
        self, text: str, window: tuple[datetime, datetime], limit: int
    ) -> list[GraphHit]:
        """Overlap, not containment — matching the real adapter's filter.

        A fact valid from January with no end date is still true in March. Testing
        against a containment fake would let an overlap bug pass.

        Also enforces the empty-query gate that the real `graphiti_core.search()`
        applies before it even looks at the filter. A caller passing `text=""` here
        raises, the same as it would against real Neo4j — this is the fake's
        replication of the bug that let `search_temporal` and `traverse` silently
        return nothing forever until it was caught by reading the vendor source.
        """
        self._guard("temporal")
        if not text.strip():
            raise ValueError("empty query text; Graphiti returns nothing for this")
        start, end = window
        found = [
            h
            for h in self.hits
            if (h.valid_from is None or h.valid_from <= end)
            and (h.valid_to is None or h.valid_to > start)
        ]
        return found[:limit]

    async def traverse(
        self, text: str, seed_ref: str, depth: int, edge_types: list[str] | None = None
    ) -> list[GraphHit]:
        """See `search_temporal` above re: the empty-query gate."""
        self._guard("traversal")
        if not text.strip():
            raise ValueError("empty query text; Graphiti returns nothing for this")
        return self.adjacency.get(seed_ref, [])

    async def rerank(self, query: str, hits: list[GraphHit]) -> list[GraphHit]:
        self.reranked_with.append((query, len(hits)))
        return sorted(hits, key=lambda h: h.score, reverse=True)

    # -------------------------------------------------------- administration

    async def invalidate_edge(self, ref: str, at: datetime) -> None:
        self.invalidated.append((ref, at))

    async def entity_divergence(self) -> list[EntityDivergence]:
        return []

    async def clear_all(self) -> None:
        # Safe here for the same reason it is safe in production: PostgreSQL is
        # the system of record and the graph is rebuildable (ADR-005).
        self.hits.clear()
        self.episodes.clear()
        self.entity_names.clear()
        self.adjacency.clear()
        self.cleared = True

    async def health(self) -> bool:
        return self._healthy
