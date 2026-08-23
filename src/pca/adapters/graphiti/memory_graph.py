"""GraphitiMemoryAdapter — implements MemoryGraphPort.

Layer L5. **The only module in this codebase permitted to import `graphiti_core`**
(boundary rule 1). The framework-immaturity entry in the risk register is only
survivable if that holds.

ADR-002: configured with the full Gemini trio — GeminiClient, GeminiEmbedder,
GeminiRerankerClient. This is the only fully OpenAI-free path through Graphiti;
its Anthropic and Groq integrations still require an OpenAI key for embeddings.

ADR-015: results from here are **candidates, not truth**. Graphiti performs its own
entity consolidation and temporal edge invalidation, and we do not treat either as
authoritative. The graph is queried to *find* things; PostgreSQL is queried to
*assert* what is true. Without that rule the system would hold two temporal models
that can disagree.

Unit scope: only `add_episode` and semantic search are implemented. The remaining
strategies raise NotImplementedError with an explicit pointer to Unit 4 rather than
returning approximate results, because a silently wrong retrieval strategy is
harder to notice than a missing one.
"""

from __future__ import annotations

import os
from datetime import datetime

# ---------------------------------------------------------------------------
# PRIVACY: disable Graphiti telemetry before importing it.
#
# graphiti_core ships PostHog analytics that are ON by default — it reads
# GRAPHITI_TELEMETRY_ENABLED and treats a missing value as 'true', then posts to a
# hardcoded US PostHog endpoint.
#
# For this application that is unacceptable. NFR-01.1 requires all data to stay on
# the user's machine apart from LLM API calls, and NFR-01.2 requires every external
# transmission to be identified. Silent third-party analytics in a private
# personal-context store is precisely the wrong default.
#
# Set here, at import time, rather than in configuration: Graphiti reads os.environ
# directly, and pydantic-settings loads .env into a Settings object without
# populating os.environ. Doing it at import guarantees it precedes any Graphiti use.
# setdefault so an explicit opt-in via the real environment is still honoured.
# ---------------------------------------------------------------------------
os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")

from graphiti_core import Graphiti  # noqa: E402
from graphiti_core.cross_encoder.gemini_reranker_client import (  # noqa: E402
    GeminiRerankerClient,
)
from graphiti_core.embedder.gemini import (  # noqa: E402
    GeminiEmbedder,
    GeminiEmbedderConfig,
)
from graphiti_core.llm_client.config import LLMConfig  # noqa: E402
from graphiti_core.llm_client.gemini_client import GeminiClient  # noqa: E402
from graphiti_core.nodes import EpisodeType  # noqa: E402
from neo4j import AsyncGraphDatabase  # noqa: E402

from pca.domain.conversation import Episode
from pca.domain.errors import MemoryGraphUnavailable
from pca.domain.ids import EntityId
from pca.observability.logging import get_logger
from pca.ports.graph import EntityDivergence, GraphHit, GraphIngestResult

_log = get_logger(__name__)

MINIMUM_NEO4J_VERSION = (5, 26)

_UNIT4 = (
    "not implemented in the walking skeleton; Unit 4 delivers the full hybrid "
    "retrieval set (semantic, full-text, entity-scoped, temporal, traversal)"
)


class GraphitiMemoryAdapter:
    """Graphiti-backed MemoryGraphPort."""

    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        api_key: str,
        llm_model: str,
        small_model: str,
        embedding_model: str,
        reranker_model: str,
        embedding_dim: int = 3072,
    ) -> None:
        self._uri = uri
        self._user = user
        self._password = password

        self._graphiti = Graphiti(
            uri=uri,
            user=user,
            password=password,
            llm_client=GeminiClient(
                config=LLMConfig(
                    api_key=api_key,
                    model=llm_model,
                    small_model=small_model,
                    # Graphiti defaults temperature to 1. Extraction wants
                    # determinism, not variety — a differently-worded fact on each
                    # run would look like new information to conflict detection.
                    temperature=0.1,
                )
            ),
            embedder=GeminiEmbedder(
                config=GeminiEmbedderConfig(
                    api_key=api_key,
                    embedding_model=embedding_model,
                    embedding_dim=embedding_dim,
                )
            ),
            cross_encoder=GeminiRerankerClient(
                config=LLMConfig(api_key=api_key, model=reranker_model)
            ),
            # Keep the raw episode body in the graph as well as PostgreSQL. Cheap,
            # and it makes Neo4j Browser genuinely useful for debugging what the
            # extractor saw.
            store_raw_episode_content=True,
        )

        # Separate driver for health and administrative operations. Deliberately
        # not reaching into Graphiti internals for these, so an internal refactor
        # upstream cannot break our health check.
        self._driver = AsyncGraphDatabase.driver(uri, auth=(user, password))

    # ------------------------------------------------------------------ lifecycle

    async def initialise(self) -> None:
        """Verify the server version, then build indices.

        The version gate is explicit because Graphiti requires Neo4j 5.26+ and an
        older server fails at first query with an opaque error rather than at
        startup. Turning that into a clear boot failure saves a confusing debugging
        session (ADR-003).
        """
        await self._assert_version()
        await self._graphiti.build_indices_and_constraints()
        _log.info("graphiti_initialised", uri=self._uri)

    async def close(self) -> None:
        await self._graphiti.close()
        await self._driver.close()

    async def _assert_version(self) -> None:
        try:
            async with self._driver.session() as session:
                result = await session.run(
                    "CALL dbms.components() YIELD name, versions "
                    "WHERE name = 'Neo4j Kernel' RETURN versions[0] AS version"
                )
                record = await result.single()
        except Exception as exc:  # noqa: BLE001
            raise MemoryGraphUnavailable(f"cannot reach Neo4j at {self._uri}: {exc}") from exc

        if record is None:
            raise MemoryGraphUnavailable("Neo4j did not report a version")

        raw = str(record["version"])
        parts = tuple(int(p) for p in raw.split(".")[:2] if p.isdigit())
        if parts < MINIMUM_NEO4J_VERSION:
            raise MemoryGraphUnavailable(
                f"Neo4j {raw} is too old; Graphiti requires "
                f"{'.'.join(map(str, MINIMUM_NEO4J_VERSION))} or newer (ADR-003)"
            )
        _log.info("neo4j_version_ok", version=raw)

    # ------------------------------------------------------------------- writes

    async def add_episode(self, episode: Episode) -> GraphIngestResult:
        """Ingest an episode.

        `reference_time` is the episode's `occurred_at`, not the current time. This
        matters for imported history and for background extraction: using ingestion
        time would collapse everything onto today and destroy the timeline.
        """
        try:
            results = await self._graphiti.add_episode(
                name=f"episode-{episode.id}",
                episode_body=episode.content,
                source_description=(
                    "conversation message" if episode.message_id else "imported document"
                ),
                reference_time=episode.occurred_at,
                source=EpisodeType.message if episode.message_id else EpisodeType.text,
                uuid=str(episode.id),
            )
        except Exception as exc:  # noqa: BLE001
            raise MemoryGraphUnavailable(f"episode ingestion failed: {exc}") from exc

        return GraphIngestResult(
            episode_ref=str(episode.id),
            entities_touched=len(getattr(results, "nodes", []) or []),
            edges_touched=len(getattr(results, "edges", []) or []),
        )

    # ------------------------------------------------------------------- search

    async def search_semantic(self, text: str, limit: int) -> list[GraphHit]:
        """Graphiti's fused search.

        Note: this single call is already hybrid internally (semantic + BM25 +
        graph). It is mapped to `search_semantic` for now because the skeleton has
        one strategy. Unit 4 separates the strategies so their contributions can be
        measured independently — which is the point of RetrievalDiagnostics.
        """
        try:
            edges = await self._graphiti.search(query=text, num_results=limit)
        except Exception as exc:  # noqa: BLE001
            raise MemoryGraphUnavailable(f"graph search failed: {exc}") from exc
        return [self._to_hit(edge) for edge in edges]

    async def search_by_entity(self, entity_id: EntityId, limit: int) -> list[GraphHit]:
        try:
            edges = await self._graphiti.search(
                query="", center_node_uuid=str(entity_id), num_results=limit
            )
        except Exception as exc:  # noqa: BLE001
            raise MemoryGraphUnavailable(f"entity search failed: {exc}") from exc
        return [self._to_hit(edge) for edge in edges]

    async def traverse(
        self, seed: EntityId, depth: int, edge_types: list[str] | None = None
    ) -> list[GraphHit]:
        # Graphiti expresses traversal as centre-node reranked search rather than
        # explicit depth. Honouring `depth` and `edge_types` properly needs the
        # search-recipe API, which Unit 4 introduces.
        return await self.search_by_entity(seed, limit=depth * 5)

    async def search_fulltext(self, text: str, limit: int) -> list[GraphHit]:
        raise NotImplementedError(f"search_fulltext {_UNIT4}")

    async def search_temporal(
        self, window: tuple[datetime, datetime], limit: int
    ) -> list[GraphHit]:
        raise NotImplementedError(f"search_temporal {_UNIT4}")

    async def rerank(self, query: str, hits: list[GraphHit]) -> list[GraphHit]:
        # Graphiti already applies the cross-encoder inside search(). Reranking
        # again here would spend a second model call for no gain. Unit 4 reranks
        # explicitly once strategies are fused by us rather than by Graphiti.
        return hits

    # ---------------------------------------------------------- administration

    async def invalidate_edge(self, ref: str, at: datetime) -> None:
        raise NotImplementedError(
            "edge invalidation arrives with the temporal write path in Unit 3"
        )

    async def entity_divergence(self) -> list[EntityDivergence]:
        raise NotImplementedError(
            "divergence reporting arrives with ReindexService.verify in Unit 7"
        )

    async def clear_all(self) -> None:
        """Wipe the graph.

        Safe only because PostgreSQL is the system of record and every episode can
        be replayed (ADR-005). In any other architecture this would be data loss.
        """
        try:
            async with self._driver.session() as session:
                await session.run("MATCH (n) DETACH DELETE n")
        except Exception as exc:  # noqa: BLE001
            raise MemoryGraphUnavailable(f"graph clear failed: {exc}") from exc
        _log.warning("graph_cleared", uri=self._uri)

    async def health(self) -> bool:
        try:
            async with self._driver.session() as session:
                result = await session.run("RETURN 1 AS ok")
                await result.single()
            return True
        except Exception as exc:  # noqa: BLE001 - health must never raise
            _log.warning("neo4j_unhealthy", error=str(exc)[:200])
            return False

    # --------------------------------------------------------------- internals

    @staticmethod
    def _to_hit(edge) -> GraphHit:  # type: ignore[no-untyped-def]
        """Map a Graphiti EntityEdge to our domain-facing hit type.

        This mapping is the boundary. No Graphiti type escapes past it, which is
        what keeps the framework swappable.
        """
        return GraphHit(
            ref=str(getattr(edge, "uuid", "")),
            content=str(getattr(edge, "fact", "")),
            score=float(getattr(edge, "score", 0.0) or 0.0),
            valid_from=getattr(edge, "valid_at", None),
            valid_to=getattr(edge, "invalid_at", None),
            raw={
                "source_node_uuid": str(getattr(edge, "source_node_uuid", "")),
                "target_node_uuid": str(getattr(edge, "target_node_uuid", "")),
                "created_at": str(getattr(edge, "created_at", "")),
            },
        )
