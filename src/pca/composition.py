"""Composition root — the only place that wires adapters to ports.

No service constructs its own dependencies. Everything is assembled here and
injected, which is what makes every L3 service testable against fakes and what
allows Unit 1a to have been built and verified with no database at all.

Startup order matters and each step can deliberately fail the boot:

    1. configuration            fail on missing GOOGLE_API_KEY / Neo4j password
    2. migration checksums      fail if an applied migration was edited
    3. apply pending migrations transactional, per file
    4. Neo4j version gate       fail if older than 5.26 (ADR-003)
    5. Graphiti indices         idempotent
    6. recover pending episodes re-ingest work lost to a crash (ADR-008)

Steps 2 and 4 exist because both failures otherwise surface much later as
confusing runtime errors rather than a clear startup message.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pca.adapters.clock.system_clock import SystemClockAdapter
from pca.adapters.gemini.provider import GeminiProviderAdapter
from pca.adapters.graphiti.memory_graph import GraphitiMemoryAdapter
from pca.adapters.postgres.conversation_repository import PostgresConversationRepository
from pca.adapters.postgres.episode_repository import PostgresEpisodeRepository
from pca.adapters.postgres.history_repositories import (
    PostgresBeliefRepository,
    PostgresOperationLogRepository,
)
from pca.adapters.postgres.memory_repositories import (
    PostgresEntityRepository,
    PostgresMemoryRepository,
    PostgresProvenanceRepository,
)
from pca.adapters.postgres.store import PostgresStoreAdapter
from pca.config.migrations import MigrationRunner
from pca.config.schema_drift import SchemaDriftCheck
from pca.config.settings import Settings, get_settings
from pca.observability.logging import configure_logging, get_logger
from pca.orchestration.conversation_workflow import ConversationWorkflow
from pca.domain.retrieval import RetrievalBudget
from pca.services.belief_history import BeliefHistoryService
from pca.services.budget import DEFAULT_BUDGET, RetrievalBudgetGovernor
from pca.services.conflicts import ConflictDetectionService
from pca.services.context_assembly import ContextAssemblyService
from pca.services.conversation import ConversationService
from pca.services.entities import EntityService
from pca.services.episodes import EpisodeService
from pca.services.extraction import ExtractionService
from pca.services.memory import MemoryService
from pca.services.operation_log import MemoryOperationLog
from pca.services.provenance import ProvenanceService
from pca.services.retrieval import RetrievalService
from pca.services.salience import SalienceScorer
from pca.services.time_resolver import TimeResolver
from pca.services.timeline import TimelineService

_log = get_logger(__name__)


@dataclass
class Container:
    """Assembled application. Held on app.state and injected into routers."""

    settings: Settings
    clock: SystemClockAdapter
    store: PostgresStoreAdapter
    graph: GraphitiMemoryAdapter
    provider: GeminiProviderAdapter
    migrations: MigrationRunner
    schema_drift: SchemaDriftCheck
    conversations: ConversationService
    episodes: EpisodeService
    extraction: ExtractionService
    retrieval: RetrievalService
    assembly: ContextAssemblyService
    conversation_workflow: ConversationWorkflow
    entities: EntityService
    provenance: ProvenanceService
    memory: MemoryService
    beliefs: BeliefHistoryService
    operations: MemoryOperationLog
    timeline: TimelineService
    conflicts: ConflictDetectionService


def build_container(settings: Settings | None = None) -> Container:
    """Wire everything. Performs no I/O — see `start` for that."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    # Validate secrets before constructing any adapter. The Gemini clients are built
    # eagerly, and without this the failure surfaces as an opaque ValueError from
    # deep inside the Google SDK ("No API key was provided") rather than as a clear
    # statement of which configuration value is missing.
    settings.require_runtime_secrets()

    clock = SystemClockAdapter(zone=settings.user_timezone)
    store = PostgresStoreAdapter(dsn=settings.postgres_dsn)

    graph = GraphitiMemoryAdapter(
        uri=settings.neo4j_uri,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
        api_key=settings.google_api_key,
        llm_model=settings.llm_model,
        small_model=settings.llm_small_model,
        embedding_model=settings.embedding_model,
        reranker_model=settings.reranker_model,
        timeout_seconds=settings.graph_timeout_seconds,
    )

    provider = GeminiProviderAdapter(
        api_key=settings.google_api_key,
        default_model=settings.llm_model,
        small_model=settings.llm_small_model,
        max_concurrency=settings.max_concurrent_llm_calls,
        timeout_seconds=settings.llm_timeout_seconds,
    )

    conversation_repository = PostgresConversationRepository(store)
    episode_repository = PostgresEpisodeRepository(store, clock)
    entity_repository = PostgresEntityRepository(store)
    memory_repository = PostgresMemoryRepository(store, clock)
    provenance_repository = PostgresProvenanceRepository(store)
    belief_repository = PostgresBeliefRepository(store)
    operation_repository = PostgresOperationLogRepository(store)

    conversations = ConversationService(repository=conversation_repository, clock=clock)

    entities = EntityService(repository=entity_repository, clock=clock)
    provenance = ProvenanceService(
        repository=provenance_repository,
        conversations=conversation_repository,
        clock=clock,
    )
    beliefs = BeliefHistoryService(repository=belief_repository, clock=clock)
    operations = MemoryOperationLog(repository=operation_repository, clock=clock)
    memory = MemoryService(
        repository=memory_repository,
        entities=entities,
        provenance=provenance,
        clock=clock,
        # The store itself satisfies TransactionManagerPort structurally, so the
        # commit boundary needs no extra adapter.
        transactions=store,
        beliefs=beliefs,
        operations=operations,
    )
    timeline = TimelineService(
        memory=memory_repository, beliefs=belief_repository, clock=clock
    )
    conflicts = ConflictDetectionService(
        memory=memory_repository,
        llm=provider,
        # The small model. Conflict classification is a two-statement comparison —
        # the same shape of work as salience, and it does not need the large model.
        classifier_model=settings.llm_small_model,
    )
    episodes = EpisodeService(
        repository=episode_repository,
        graph=graph,
        clock=clock,
        llm_model=settings.llm_model,
        embedding_model=settings.embedding_model,
    )
    extraction = ExtractionService(
        provider=provider,
        resolver=TimeResolver(),
        salience=SalienceScorer(),
        model=settings.llm_model,
    )
    governor = RetrievalBudgetGovernor(
        RetrievalBudget(
            max_duration=settings.retrieval_budget,
            max_items=DEFAULT_BUDGET.max_items,
            max_context_chars=DEFAULT_BUDGET.max_context_chars,
        )
    )
    retrieval = RetrievalService(
        graph=graph,
        # ADR-015: the graph finds candidates, PostgreSQL asserts what is true.
        # Without these repositories retrieval would return graph paraphrases as
        # though they were remembered facts.
        memory=memory_repository,
        entities=entity_repository,
        beliefs=belief_repository,
        governor=governor,
    )
    assembly = ContextAssemblyService(provenance=provenance)

    workflow = ConversationWorkflow(
        conversations=conversations,
        retrieval=retrieval,
        assembly=assembly,
        provider=provider,
        model=settings.llm_model,
    )

    return Container(
        settings=settings,
        clock=clock,
        store=store,
        graph=graph,
        provider=provider,
        migrations=MigrationRunner(store, clock, Path("migrations")),
        schema_drift=SchemaDriftCheck(store),
        conversations=conversations,
        episodes=episodes,
        extraction=extraction,
        retrieval=retrieval,
        assembly=assembly,
        conversation_workflow=workflow,
        entities=entities,
        provenance=provenance,
        memory=memory,
        beliefs=beliefs,
        operations=operations,
        timeline=timeline,
        conflicts=conflicts,
    )


async def start(container: Container) -> None:
    """Run the startup sequence. Raises rather than degrading."""
    container.settings.require_runtime_secrets()

    await container.migrations.verify_checksums()
    applied = await container.migrations.apply_pending()
    if applied:
        _log.info("migrations_applied", versions=[a.version for a in applied])

    # Compare declared table metadata against the live schema. This is the safety
    # property that not using Alembic would otherwise cost: without it, a column
    # declared in tables.py but missing from every migration fails at whichever query
    # touches it first, potentially weeks later.
    await container.schema_drift.assert_matches()

    await container.graph.initialise()

    recovered = await container.episodes.recover_pending()
    if recovered:
        _log.info("pending_episodes_recovered", count=len(recovered))

    _log.info("application_started", llm=container.settings.llm_model)


async def stop(container: Container) -> None:
    await container.graph.close()
    await container.store.close()
    _log.info("application_stopped")
