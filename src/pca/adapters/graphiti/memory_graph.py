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

All five retrieval strategies are implemented as of Unit 4, each built from an
explicit `SearchConfig` so their contributions are genuinely distinct and separately
measurable. Unit 1b mapped a single fused Graphiti call to `search_semantic`; that
call was internally hybrid, so per-strategy diagnostics were meaningless.

Each strategy uses `rrf` reranking internally rather than `cross_encoder`. We fuse
across strategies ourselves and cross-encode once at the end (`rerank`); letting
Graphiti cross-encode per strategy would multiply reranker cost by five and produce
rankings we immediately discard.
"""

from __future__ import annotations

import asyncio
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
from graphiti_core.search.search_config import (  # noqa: E402
    EdgeReranker,
    EdgeSearchConfig,
    EdgeSearchMethod,
    NodeReranker,
    NodeSearchConfig,
    NodeSearchMethod,
    SearchConfig,
)
from graphiti_core.search.search_filters import (  # noqa: E402
    ComparisonOperator,
    DateFilter,
    SearchFilters,
)
from neo4j import AsyncGraphDatabase  # noqa: E402

from pca.adapters.graphiti.entity_types import GRAPHITI_ENTITY_TYPES  # noqa: E402
from pca.domain.conversation import Episode  # noqa: E402
from pca.domain.errors import MemoryGraphUnavailable  # noqa: E402
from pca.domain.ids import EntityId  # noqa: E402
from pca.observability.logging import get_logger  # noqa: E402
from pca.ports.graph import (  # noqa: E402
    EntityDivergence,
    GraphHit,
    GraphIngestResult,
)

_log = get_logger(__name__)

MINIMUM_NEO4J_VERSION = (5, 26)


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
        graphiti: object | None = None,
        driver: object | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._uri = uri
        self._user = user
        self._password = password
        # RESILIENCY-10. Before Unit 5 this adapter had no timeout of any kind, so a
        # hung Neo4j or Gemini call inside Graphiti waited forever. The retrieval
        # budget governor masked it on the read path; nothing masked the write path.
        self._timeout = timeout_seconds

        # `graphiti` and `driver` are injectable purely for testing. The uuid defect
        # that silently disabled the whole memory pipeline was undetectable without
        # a seam here, because the adapter constructed its own client and could only
        # be exercised against a live Neo4j.
        if graphiti is not None:
            self._graphiti = graphiti  # type: ignore[assignment]
            self._driver = driver  # type: ignore[assignment]
            return

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

        **Do not pass `uuid`.** Graphiti's `uuid` parameter means "update the
        existing episode with this id" — it calls `EpisodicNode.get_by_uuid` and
        raises "node not found" when the id is absent. It is not a way to assign an
        id to a new episode.

        Passing our PostgreSQL episode id here was the defect that made the entire
        memory pipeline silently store nothing: every ingestion failed, retrieval
        searched an empty graph, and the assistant truthfully reported having no
        history. Graphiti assigns its own node id, returned as `episode_ref`.
        """
        try:
            results = await self._guard(
                self._graphiti.add_episode(
                    name=f"episode-{episode.id}",
                    episode_body=episode.content,
                    source_description=(
                        "conversation message"
                        if episode.message_id
                        else "imported document"
                    ),
                    reference_time=episode.occurred_at,
                    source=(
                        EpisodeType.message if episode.message_id else EpisodeType.text
                    ),
                    # ADR-015: prescribe the ontology rather than letting Graphiti
                    # infer categories freely, so its graph stays legible against our
                    # own EntityType and divergence stays small.
                    entity_types=GRAPHITI_ENTITY_TYPES,
                ),
                "episode ingestion",
            )
        except Exception as exc:  # noqa: BLE001
            raise MemoryGraphUnavailable(f"episode ingestion failed: {exc}") from exc

        graph_uuid = str(getattr(getattr(results, "episode", None), "uuid", "") or "")
        nodes = list(getattr(results, "nodes", None) or [])
        edges = list(getattr(results, "edges", None) or [])

        # An ingestion that extracts nothing is not an error, but it is worth
        # seeing: it usually means the message carried no durable content, and a
        # persistent run of zeros would indicate extraction is broken.
        _log.info(
            "graph_episode_added",
            episode_id=str(episode.id),
            graph_uuid=graph_uuid,
            entities=len(nodes),
            edges=len(edges),
        )

        return GraphIngestResult(
            episode_ref=graph_uuid or str(episode.id),
            entities_touched=len(nodes),
            edges_touched=len(edges),
        )

    # ------------------------------------------------------------------- search

    async def search_semantic(self, text: str, limit: int) -> list[GraphHit]:
        """Embedding similarity only — no keyword matching.

        Narrowed from Unit 1b, which called Graphiti's default `search()`. That call
        was internally hybrid (cosine + BM25 + BFS), so attributing its results to
        "semantic" made per-strategy diagnostics fiction: full-text and traversal
        were already folded in and could not be measured or disabled independently.
        """
        config = SearchConfig(
            edge_config=EdgeSearchConfig(
                search_methods=[EdgeSearchMethod.cosine_similarity],
                reranker=EdgeReranker.rrf,
            ),
            limit=limit,
        )
        return await self._search_edges(text, config, "semantic search")

    async def search_fulltext(self, text: str, limit: int) -> list[GraphHit]:
        """BM25 only — keyword matching, no embeddings.

        Genuinely distinct from `search_semantic` rather than a second call to the
        same thing. Exact terms are where embeddings are weakest: a rare surname or
        a project codename may have almost no semantic neighbourhood, and cosine
        similarity will happily rank a topically-similar-but-wrong fact above the
        one containing the literal token. Keyword search is what catches those.

        Uses `rrf` reranking rather than `cross_encoder` because this strategy's
        output is fused by us afterwards; letting Graphiti spend cross-encoder calls
        per strategy would multiply the reranking cost by five for a ranking we then
        discard.
        """
        config = SearchConfig(
            edge_config=EdgeSearchConfig(
                search_methods=[EdgeSearchMethod.bm25],
                reranker=EdgeReranker.rrf,
            ),
            limit=limit,
        )
        return await self._search_edges(text, config, "fulltext search")

    async def search_by_entity(self, entity_name: str, limit: int) -> list[GraphHit]:
        """Facts about one entity, located by name.

        Two steps, because Graphiti's node ids are not ours (see the port docstring).
        First find the graph's node for this name, then centre an edge search on it.
        Returns empty rather than raising when the name is not in the graph — an
        entity we know about that Graphiti has not extracted is an expected
        divergence (ADR-015), not a failure.
        """
        node_uuid = await self._node_uuid_for(entity_name)
        if node_uuid is None:
            _log.info("entity_not_in_graph", name=entity_name)
            return []

        config = SearchConfig(
            edge_config=EdgeSearchConfig(
                search_methods=[
                    EdgeSearchMethod.bm25,
                    EdgeSearchMethod.cosine_similarity,
                ],
                reranker=EdgeReranker.node_distance,
            ),
            limit=limit,
        )
        try:
            results = await self._guard(
                self._graphiti.search_(
                    query=entity_name, config=config, center_node_uuid=node_uuid
                ),
                "entity search",
            )
        except Exception as exc:  # noqa: BLE001
            raise MemoryGraphUnavailable(f"entity search failed: {exc}") from exc
        return [self._to_hit(edge) for edge in results.edges]

    async def search_temporal(
        self, text: str, window: tuple[datetime, datetime], limit: int
    ) -> list[GraphHit]:
        """Edges relevant to `text` whose validity overlaps the window.

        **`text` must not be empty.** `graphiti_core.search.search.search()` checks
        `query.strip() == ""` and returns an empty `SearchResults()` before it even
        inspects `config` — the date filter below never runs against an empty
        string. An earlier version of this method took no text argument and passed
        `""`, which passed every offline test (the fake graph has no such gate) and
        would have silently returned zero results against real Neo4j forever.

        The filter itself expresses overlap, not containment: an edge valid from
        January with no end date is still true during March. Filtering on
        `valid_at` falling inside the window would miss exactly the long-running
        facts most worth retrieving — where someone lives, who they work for.

        SearchFilters nests as OR-of-AND: the outer list is disjunction, each inner
        list conjunction.
        """
        if not text.strip():
            raise ValueError(
                "search_temporal requires non-empty text; Graphiti's search() "
                "returns nothing for an empty query regardless of filters"
            )
        start, end = window
        config = SearchConfig(
            edge_config=EdgeSearchConfig(
                search_methods=[EdgeSearchMethod.bm25, EdgeSearchMethod.cosine_similarity],
                reranker=EdgeReranker.rrf,
            ),
            limit=limit,
        )
        filters = SearchFilters(
            # Began at or before the window ended.
            valid_at=[
                [DateFilter(date=end, comparison_operator=ComparisonOperator.less_than_equal)]
            ],
            # Ended after the window began, or never ended.
            invalid_at=[
                [
                    DateFilter(
                        date=start, comparison_operator=ComparisonOperator.greater_than
                    )
                ],
                [DateFilter(comparison_operator=ComparisonOperator.is_null)],
            ],
        )
        try:
            results = await self._guard(
                self._graphiti.search_(
                    query=text, config=config, search_filter=filters
                ),
                "temporal search",
            )
        except Exception as exc:  # noqa: BLE001
            raise MemoryGraphUnavailable(f"temporal search failed: {exc}") from exc
        return [self._to_hit(edge) for edge in results.edges]

    async def traverse(
        self, text: str, seed_ref: str, depth: int, edge_types: list[str] | None = None
    ) -> list[GraphHit]:
        """Breadth-first expansion from a seed node.

        `depth` maps onto Graphiti's `bfs_max_depth`. This is the one strategy that
        genuinely follows graph structure rather than matching text, which is what
        finds the fact nobody asked about directly — "who is Priya's employer" when
        the question was about Bangalore.

        `text` is required even though `edge_bfs_search` itself never reads it —
        see the port docstring. Graphiti's `search()` gates on the query string
        before dispatching to any search method, BFS included.
        """
        if not text.strip():
            raise ValueError(
                "traverse requires non-empty text; Graphiti's search() returns "
                "nothing for an empty query regardless of the BFS config"
            )
        config = SearchConfig(
            edge_config=EdgeSearchConfig(
                search_methods=[EdgeSearchMethod.bfs],
                reranker=EdgeReranker.rrf,
                bfs_max_depth=max(1, depth),
            ),
            limit=depth * 5,
        )
        filters = (
            SearchFilters(edge_types=edge_types) if edge_types else SearchFilters()
        )
        try:
            results = await self._guard(
                self._graphiti.search_(
                    query=text,
                    config=config,
                    bfs_origin_node_uuids=[seed_ref],
                    search_filter=filters,
                ),
                "traversal",
            )
        except Exception as exc:  # noqa: BLE001
            raise MemoryGraphUnavailable(f"traversal failed: {exc}") from exc
        return [self._to_hit(edge) for edge in results.edges]

    async def rerank(self, query: str, hits: list[GraphHit]) -> list[GraphHit]:
        """Cross-encode the fused set.

        Now that WE fuse the strategies rather than Graphiti, its internal
        cross-encoder no longer sees the combined candidate set — it only ever ranked
        within one strategy. This is the single place the reranker sees everything.

        **Cost warning:** `GeminiRerankerClient.rank` issues one API call per
        passage. The caller must cap the input (RetrievalBudgetGovernor.rerank_cutoff
        exists for this). Reranking an uncapped fused set is the fastest way to blow
        the latency budget.

        Falls back to the input order on failure. A degraded ranking is a far better
        outcome than a failed request, and the fused order is already reasonable.
        """
        if len(hits) <= 1:
            return hits

        by_content: dict[str, GraphHit] = {}
        for hit in hits:
            # Duplicate content would make the returned passage ambiguous when
            # mapping scores back. Keep the first occurrence.
            by_content.setdefault(hit.content, hit)

        passages = list(by_content)
        if len(passages) <= 1:
            return hits

        try:
            ranked = await self._guard(
                self._graphiti.cross_encoder.rank(query, passages), "rerank"
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "rerank_failed",
                error=str(exc)[:200],
                consequence="falling back to fused order",
            )
            return hits

        ordered: list[GraphHit] = []
        for passage, score in ranked:
            hit = by_content.get(passage)
            if hit is not None:
                # Carry the cross-encoder score forward; it is more meaningful than
                # the per-strategy score it replaces.
                ordered.append(
                    GraphHit(
                        ref=hit.ref,
                        content=hit.content,
                        score=score,
                        valid_from=hit.valid_from,
                        valid_to=hit.valid_to,
                        entity_ids=hit.entity_ids,
                        raw=hit.raw,
                    )
                )
        return ordered or hits

    # ---------------------------------------------------------- administration

    async def invalidate_edge(self, ref: str, at: datetime) -> None:
        # Not wired even though Unit 3 landed. MemoryService.correct/supersede/
        # retract operate on PostgreSQL only (ADR-005/015) and never call this —
        # Graphiti's own temporal invalidation runs independently inside its
        # extraction pipeline. Left unimplemented until a concrete caller exists
        # rather than guessing at a signature nothing uses yet.
        raise NotImplementedError(
            "no caller yet; PostgreSQL corrections do not propagate to graph edges"
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

    async def _search_edges(
        self, query: str, config: SearchConfig, label: str
    ) -> list[GraphHit]:
        try:
            results = await self._guard(
                self._graphiti.search_(query=query, config=config), label
            )
        except Exception as exc:  # noqa: BLE001
            raise MemoryGraphUnavailable(f"{label} failed: {exc}") from exc
        return [self._to_hit(edge) for edge in results.edges]

    async def _guard(self, awaitable, label: str):  # type: ignore[no-untyped-def]
        """Bound a Graphiti call in time.

        Translated to `MemoryGraphUnavailable` rather than surfacing a raw timeout,
        because the graph IS degradable (ADR-005) — it is a rebuildable projection
        and PostgreSQL still holds every fact. Callers already handle this error by
        narrowing retrieval and disclosing, which is the correct response to a
        timeout as much as to a refused connection.
        """
        try:
            return await asyncio.wait_for(awaitable, timeout=self._timeout)
        except TimeoutError as exc:
            _log.error("graph_timeout", operation=label, seconds=self._timeout)
            raise MemoryGraphUnavailable(
                f"{label} exceeded {self._timeout}s"
            ) from exc

    async def _node_uuid_for(self, name: str) -> str | None:
        """Find Graphiti's own node id for an entity name.

        Needed because our EntityId is not Graphiti's node uuid. Matches on exact
        name first and only falls back to the top-ranked node, so a search for
        "Priya" does not silently centre on "Priyanka" — a wrong centre node returns
        confidently scoped results about the wrong person, which is worse than
        returning nothing.
        """
        config = SearchConfig(
            node_config=NodeSearchConfig(
                search_methods=[NodeSearchMethod.bm25, NodeSearchMethod.cosine_similarity],
                reranker=NodeReranker.rrf,
            ),
            limit=5,
        )
        try:
            results = await self._guard(
                self._graphiti.search_(query=name, config=config), "node lookup"
            )
        except Exception as exc:  # noqa: BLE001
            raise MemoryGraphUnavailable(f"node lookup failed: {exc}") from exc

        nodes = list(getattr(results, "nodes", None) or [])
        if not nodes:
            return None

        wanted = name.strip().casefold()
        for node in nodes:
            if str(getattr(node, "name", "")).strip().casefold() == wanted:
                return str(getattr(node, "uuid", "")) or None

        _log.info(
            "entity_name_no_exact_graph_match",
            name=name,
            candidates=[str(getattr(n, "name", "")) for n in nodes[:3]],
            action="scoped search skipped rather than centring on a near match",
        )
        return None

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
