"""ExtractionWorkflow — the background write path.

Layer L2. Imports `langgraph`; per ADR-006 every node body is a service call plus
state mapping and contains no business logic.

This is where the memory-write sequence lives now. Until Unit 5 it sat inline in
`api/conversation.py`, executed synchronously inside the SSE response — which is the
NFR-02.3 violation carried since Unit 1b. It has been **moved** here rather than
copied; leaving a second copy behind is how the two drift and a fix lands in only one.

Node order follows `services.md` Workflow 2, and one position in it is load-bearing:
conflict detection runs **between extraction and commit**. Running it afterwards
would mean the store already held both versions with no record that they disagree,
which is precisely the state FR-05 exists to prevent.

Graph ingestion moved here too. The router now persists the episode to PostgreSQL and
returns; ingestion is a Graphiti call that fans out to Gemini, so leaving it on the
request path would have kept most of the latency this unit exists to remove. ADR-005's
ordering is preserved either way — PostgreSQL first, graph second.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from pca.domain.conversation import Episode
from pca.domain.enums import ExtractionState
from pca.domain.extraction import ExtractionCandidates
from pca.domain.ids import EpisodeId
from pca.domain.memory import CommitReceipt, Conflict
from pca.domain.orchestration import ExtractionOutcome
from pca.observability.logging import get_logger
from pca.ports.repositories import EpisodeRepositoryPort
from pca.services.conflicts import ConflictDetectionService
from pca.services.episodes import EpisodeService
from pca.services.extraction import ExtractionService
from pca.services.memory import MemoryService
from pca.services.provenance import ProvenanceService

_log = get_logger(__name__)


def _keep_last(_existing: object, new: object) -> object:
    return new


class ExtractionGraphState(TypedDict, total=False):
    episode_id: EpisodeId
    episode: Annotated[Episode, _keep_last]
    candidates: Annotated[ExtractionCandidates, _keep_last]
    conflicts: Annotated[list[Conflict], _keep_last]
    receipt: Annotated[CommitReceipt, _keep_last]
    contradictions: Annotated[list[str], _keep_last]
    already_done: Annotated[bool, _keep_last]
    ingested: Annotated[bool, _keep_last]


class ExtractionWorkflow:
    def __init__(
        self,
        episodes: EpisodeService,
        episode_repository: EpisodeRepositoryPort,
        extraction: ExtractionService,
        conflicts: ConflictDetectionService,
        memory: MemoryService,
        provenance: ProvenanceService,
    ) -> None:
        self._episodes = episodes
        self._episode_repository = episode_repository
        self._extraction = extraction
        self._conflicts = conflicts
        self._memory = memory
        self._provenance = provenance
        self._graph = self._build()

    # ------------------------------------------------------------------- graph

    def _build(self):  # type: ignore[no-untyped-def]
        builder = StateGraph(ExtractionGraphState)
        builder.add_node("load", self._load)
        builder.add_node("ingest", self._ingest)
        builder.add_node("extract", self._extract)
        builder.add_node("detect", self._detect)
        builder.add_node("commit", self._commit)
        builder.add_node("reconcile", self._reconcile)

        builder.add_edge(START, "load")
        # Skips everything downstream when this episode has already been committed.
        builder.add_conditional_edges(
            "load", self._already_committed, {True: END, False: "ingest"}
        )
        builder.add_edge("ingest", "extract")
        # An episode that yielded nothing worth remembering is not a failure, but
        # running conflict detection and a commit over an empty candidate set spends
        # a model call and a transaction to write nothing.
        builder.add_conditional_edges(
            "extract", self._nothing_extracted, {True: END, False: "detect"}
        )
        builder.add_edge("detect", "commit")
        builder.add_edge("commit", "reconcile")
        builder.add_edge("reconcile", END)

        # No checkpointer. This path has no interrupt, and its durability comes from
        # `extraction_status` rather than from graph state.
        return builder.compile()

    @staticmethod
    def _already_committed(state: ExtractionGraphState) -> bool:
        return bool(state.get("already_done"))

    @staticmethod
    def _nothing_extracted(state: ExtractionGraphState) -> bool:
        candidates = state.get("candidates")
        return candidates is None or candidates.is_empty

    # ------------------------------------------------------------------- nodes

    async def _load(self, state: ExtractionGraphState) -> ExtractionGraphState:
        """Fetch the episode and check whether its memories already exist.

        The idempotency check belongs here rather than in the coordinator, because
        the coordinator's `episode_id` primary key only prevents duplicate *submits*.
        It does nothing about the genuinely dangerous case: a crash after `commit`
        but before the status row was marked finished. Recovery re-runs that episode,
        and without this check it would write every fact a second time — the exact
        double-write ADR-008 asks us to prevent.
        """
        episode_id = state["episode_id"]
        episode = await self._episode_repository.get(episode_id)
        if episode is None:
            raise LookupError(f"no episode with id {episode_id}")

        existing = await self._provenance.memories_from_episode(episode_id)
        if existing:
            _log.info(
                "extraction_skipped_already_committed",
                episode_id=str(episode_id),
                memories=len(existing),
            )
            return {"episode": episode, "already_done": True}

        return {"episode": episode, "already_done": False}

    async def _ingest(self, state: ExtractionGraphState) -> ExtractionGraphState:
        """Push the episode into the graph.

        Non-fatal. The episode is already durable in PostgreSQL, so a graph failure
        is a retryable backlog rather than a lost memory (ADR-005). Extraction
        continues regardless: it reads the episode text, not the graph.
        """
        return {"ingested": await self._episodes.ingest(state["episode"])}

    async def _extract(self, state: ExtractionGraphState) -> ExtractionGraphState:
        return {"candidates": await self._extraction.extract(state["episode"])}

    async def _detect(self, state: ExtractionGraphState) -> ExtractionGraphState:
        return {"conflicts": await self._conflicts.detect(state["candidates"])}

    async def _commit(self, state: ExtractionGraphState) -> ExtractionGraphState:
        return {
            "receipt": await self._memory.commit(
                state["candidates"], state["episode"]
            )
        }

    async def _reconcile(self, state: ExtractionGraphState) -> ExtractionGraphState:
        """Apply the conflict branch from `services.md` Workflow 2.

        TEMPORAL_CHANGE supersedes: "she moved in March" does not make "she lives in
        Pune" false, it bounds it, and superseding keeps the earlier state queryable
        (FR-04.4). CONTRADICTION is surfaced and never resolved — FR-05.6 forbids
        silently picking a winner. AGREEMENT and REFINEMENT need no action here; the
        candidate is already committed and the corroboration shows up as an extra
        provenance link.
        """
        episode = state["episode"]
        conflicts = state.get("conflicts", [])

        for change in self._conflicts.supersessions(conflicts):
            try:
                await self._memory.supersede(
                    change.existing_memory_id,
                    new_statement=change.incoming_statement,
                    effective_from=episode.occurred_at,
                    reason=change.explanation,
                )
            except Exception as exc:  # noqa: BLE001
                # The new fact is already committed. A failed supersession leaves the
                # old one unbounded rather than losing anything, so this must not
                # fail the extraction.
                _log.warning(
                    "supersession_failed",
                    memory_id=str(change.existing_memory_id),
                    error=str(exc)[:200],
                    consequence="both states retained; old fact not bounded",
                )

        return {
            "contradictions": [
                c.explanation for c in self._conflicts.contradictions(conflicts)
            ]
        }

    # ------------------------------------------------------------------ public

    async def run(self, episode_id: EpisodeId) -> ExtractionOutcome:
        """Extract and commit one episode. Idempotent by `episode_id` (ADR-008)."""
        final: ExtractionGraphState = await self._graph.ainvoke(
            {"episode_id": episode_id}
        )

        if final.get("already_done"):
            return ExtractionOutcome(
                episode_id=episode_id,
                state=ExtractionState.SUCCEEDED,
                already_done=True,
            )

        receipt = final.get("receipt")
        if receipt is None:
            _log.info("extraction_found_nothing", episode_id=str(episode_id))
            return ExtractionOutcome(
                episode_id=episode_id, state=ExtractionState.SUCCEEDED
            )

        return ExtractionOutcome(
            episode_id=episode_id,
            state=ExtractionState.SUCCEEDED,
            facts_committed=len(receipt.fact_ids),
            contradictions=final.get("contradictions", []),
            needs_clarification=receipt.needs_clarification,
        )
