"""In-memory fake of MemoryGraphPort.

Substitutes for Graphiti and Neo4j so that services depending on the graph are
testable with no container runtime — which is the whole reason Unit 1a can
proceed while Unit 1b is blocked.

Search here is naive substring matching. It is not pretending to be Graphiti's
hybrid search; it exists to let callers be exercised, not to validate retrieval
quality. Retrieval quality is Unit 4's problem and needs the real thing.
"""

from datetime import datetime

from pca.domain.conversation import Episode
from pca.domain.ids import EntityId
from pca.ports.graph import EntityDivergence, GraphHit, GraphIngestResult


class FakeMemoryGraph:
    """Dict-backed implementation of MemoryGraphPort."""

    def __init__(self, healthy: bool = True) -> None:
        self.episodes: list[Episode] = []
        self.hits: list[GraphHit] = []
        self.invalidated: list[tuple[str, datetime]] = []
        self.cleared = False
        self._healthy = healthy

    async def add_episode(self, episode: Episode) -> GraphIngestResult:
        self.episodes.append(episode)
        self.hits.append(
            GraphHit(
                ref=f"episode:{episode.id}",
                content=episode.content,
                score=1.0,
                valid_from=episode.occurred_at,
            )
        )
        return GraphIngestResult(episode_ref=f"episode:{episode.id}")

    async def search_semantic(self, text: str, limit: int) -> list[GraphHit]:
        return self._match(text, limit)

    async def search_fulltext(self, text: str, limit: int) -> list[GraphHit]:
        return self._match(text, limit)

    async def search_by_entity(self, entity_id: EntityId, limit: int) -> list[GraphHit]:
        return [h for h in self.hits if entity_id in h.entity_ids][:limit]

    async def search_temporal(
        self, window: tuple[datetime, datetime], limit: int
    ) -> list[GraphHit]:
        start, end = window
        found = [
            h for h in self.hits if h.valid_from and start <= h.valid_from < end
        ]
        return found[:limit]

    async def traverse(
        self, seed: EntityId, depth: int, edge_types: list[str] | None = None
    ) -> list[GraphHit]:
        return [h for h in self.hits if seed in h.entity_ids]

    async def rerank(self, query: str, hits: list[GraphHit]) -> list[GraphHit]:
        return sorted(hits, key=lambda h: h.score, reverse=True)

    async def invalidate_edge(self, ref: str, at: datetime) -> None:
        self.invalidated.append((ref, at))

    async def entity_divergence(self) -> list[EntityDivergence]:
        return []

    async def clear_all(self) -> None:
        # Safe here for the same reason it is safe in production: PostgreSQL is
        # the system of record and the graph is rebuildable (ADR-005).
        self.hits.clear()
        self.episodes.clear()
        self.cleared = True

    async def health(self) -> bool:
        return self._healthy

    # --------------------------------------------------------------- internals

    def _match(self, text: str, limit: int) -> list[GraphHit]:
        needle = text.lower()
        return [h for h in self.hits if needle in h.content.lower()][:limit]
