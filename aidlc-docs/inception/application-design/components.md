# Components

## Architectural Style

Modular monolith (NFR-05.1), organized in strict layers with a ports-and-adapters boundary at the edges.

```text
+---------------------------------------------------------------+
|  L1  API LAYER            FastAPI routers, request/response    |
+---------------------------------------------------------------+
|  L2  ORCHESTRATION        LangGraph workflows, intent routing   |
+---------------------------------------------------------------+
|  L3  DOMAIN SERVICES      Business logic, memory semantics      |
+---------------------------------------------------------------+
|  L4  PORTS                Abstract interfaces (no impl)         |
+---------------------------------------------------------------+
|  L5  ADAPTERS             Graphiti, Gemini, Postgres, files     |
+---------------------------------------------------------------+
|  L6  INFRASTRUCTURE       Neo4j 5.26+, PostgreSQL, filesystem   |
+---------------------------------------------------------------+
```

### Layering rules

These are enforceable constraints, not suggestions. A violation is a design defect.

1. A layer may depend only on the layer directly below it, plus L4 ports and cross-cutting concerns.
2. L3 domain services depend on **ports only**, never on adapters. Adapters are injected at composition time.
3. LangGraph types must not appear above L2 or below L2. The graph definition is thin; nodes delegate to L3 services.
4. Graphiti types must not escape the `GraphitiMemoryAdapter`. Domain code speaks in our own domain models.
5. Nothing below L1 knows about HTTP.

---

## L1 — API Layer

| Component | Responsibility |
|---|---|
| **ConversationRouter** | Accept user messages, stream responses, list and fetch conversations. The only entry point for conversational interaction. |
| **MemoryInspectionRouter** | Read-only access to stored memory: search facts, browse entities, fetch relationships, resolve provenance chains, query timelines. |
| **MemoryManagementRouter** | Mutating memory operations: correct, supersede, delete, and explicit remember. Separated from inspection so read and write surfaces have distinct shapes. |
| **ImportRouter** | Accept text/markdown documents for ingestion into memory. |
| **DataManagementRouter** | Export, backup, restore, and trigger reindex/rebuild. |
| **HealthRouter** | Liveness and readiness, including per-dependency health (Neo4j, PostgreSQL, Gemini reachability). |

**Interfaces**: HTTP/JSON. Server-Sent Events for streaming token output (FR-01.2). No authentication in MVP — single-user local deployment. This is a deliberate, recorded gap; the service binds to localhost only and must not be exposed to a network interface without adding auth first.

---

## L2 — Orchestration Layer

| Component | Responsibility |
|---|---|
| **IntentRouter** | Classify an inbound message and select the workflow to execute. Produces a routing decision with a confidence value, not a silent guess. |
| **ConversationWorkflow** | The normal path: understand request, retrieve context, assemble context, generate response. |
| **ExtractionWorkflow** | Background path: analyze message, extract candidate facts/events/entities/relationships, detect conflicts against existing memory, commit. |
| **CorrectionWorkflow** | Identify affected memories, resolve source records, compute the correction, apply supersession while preserving history. |
| **HistoricalAnalysisWorkflow** | Resolve a time range, retrieve events and entities in range, reconstruct timeline, reason over change. |
| **ClarificationWorkflow** | Handle uncertain or ambiguous memory. Pauses execution to ask the user, then resumes. Uses LangGraph interrupt. |
| **WorkflowCheckpointer** | Durable LangGraph state persistence, backed by PostgreSQL. Enables crash recovery and resumable interrupts. |

**Design note**: Workflows contain control flow, not business logic. Every node body is a call into an L3 service plus state mapping. This keeps the graph replaceable — the counter-argument in ADR-006 stays live, so the cost of abandoning LangGraph must remain low.

---

## L3 — Domain Services

### Conversation and source material

| Component | Responsibility |
|---|---|
| **ConversationService** | Create conversations, append messages, fetch history. Messages are append-only and never mutated (FR-01.4). |
| **ImportService** | Parse text/markdown into episode candidates, attribute them to a synthetic source document, and enqueue for extraction (FR-01.6). |

### Memory write path

| Component | Responsibility |
|---|---|
| **ExtractionService** | Turn a message or document into candidate facts, events, entities, and relationships. Tags every candidate as user-stated or AI-inferred (FR-02.4). Never promotes an inference to a fact (FR-02.7). |
| **ExtractionCoordinator** | Owns the per-conversation write barrier and the background extraction queue (ADR-008). Enforces timeout, idempotency, and durable pending state. |
| **MemoryService** | The single write path into memory. Applies create, correct, supersede, and delete operations with their temporal semantics. |
| **ConflictDetectionService** | Compare incoming candidates against existing memory. Classify as agreement, refinement, temporal change, or genuine contradiction. Surfaces contradictions rather than resolving them silently (FR-05.6). |
| **ProvenanceService** | Record and resolve the chain from any derived memory back to its source message or document (FR-02.5, FR-09.3). |
| **BeliefHistoryService** | Record what the system believed at each point in time, so "what did I think was true in March?" is answerable (FR-05.5). Distinct from event-time validity. |

### Memory read path

| Component | Responsibility |
|---|---|
| **RetrievalService** | Hybrid retrieval orchestration: semantic, full-text, entity-scoped, temporal-filtered, and graph traversal, with fusion and reranking (FR-06.2, FR-06.4). |
| **RetrievalBudgetGovernor** | Enforce the latency budget (NFR-02.1) and the context-size ceiling (FR-06.5). Decides when to stop searching rather than returning everything similar. |
| **ContextAssemblyService** | Build the explicit context package, preserving the four-way distinction between user-stated, system-derived, currently-believed, and uncertain (FR-07.2). |
| **TimelineService** | Chronological reconstruction, "what was true at T", and "what changed between T1 and T2" (FR-04.5, FR-04.6, FR-04.7). |
| **EntityService** | Entity lookup, attribute history, and merge of duplicate references to the same entity (FR-03.4). |

### Data lifecycle

| Component | Responsibility |
|---|---|
| **ExportService** | Full export of conversations, memories, and graph in human-readable form (FR-10.1, FR-10.3). |
| **BackupService** | Backup and restore of both stores (FR-10.2). |
| **ReindexService** | Rebuild Neo4j from PostgreSQL episodes. Idempotent and resumable. First-class feature per ADR-005. |
| **DeletionService** | Logical deletion with audit trail (FR-05.4), plus hard deletion for the complete-erasure case (NFR-01.6). |

---

## L4 — Ports

Abstract interfaces. No implementation, no third-party types in signatures.

| Port | Abstracts | Why it exists |
|---|---|---|
| **MemoryGraphPort** | Temporal knowledge graph operations | Confines Graphiti. The framework-immaturity risk in the register is only survivable if this boundary holds. |
| **LLMProviderPort** | Our direct LLM calls | NFR-04 provider independence. Separate from Graphiti's own provider config (ADR-007). |
| **RelationalStorePort** | PostgreSQL persistence | Keeps SQL out of domain services. |
| **ObjectStorePort** | Blob storage for imports and exports | Local filesystem in MVP; S3/MinIO swappable later without touching domain code. |
| **ClockPort** | Current time | Non-negotiable for a temporal system. Testing "what was true in March" requires controllable time. |

**On ClockPort**: this looks like over-abstraction until you try to test bi-temporal correctness. Every timestamp in the system flows from this port so that temporal scenarios are deterministic and testable.

---

## L5 — Adapters

| Adapter | Implements | Notes |
|---|---|---|
| **GraphitiMemoryAdapter** | MemoryGraphPort | Wraps `graphiti-core` configured with `GeminiClient`, `GeminiEmbedder`, `GeminiRerankerClient` (ADR-002). Translates between Graphiti's episode/edge model and our domain model. |
| **GeminiProviderAdapter** | LLMProviderPort | Google GenAI SDK. Structured output for classification and extraction schemas. Retry with backoff, and fallback behavior per NFR-06.1. |
| **PostgresStoreAdapter** | RelationalStorePort | SQLAlchemy Core expression language over asyncpg (ADR-009). |
| **LocalFileStoreAdapter** | ObjectStorePort | Filesystem-backed, path-scoped to a data directory. |
| **SystemClockAdapter** | ClockPort | Real wall clock. Test doubles substitute a fixed or scriptable clock. |

---

## Cross-Cutting Components

| Component | Responsibility |
|---|---|
| **ConfigurationManager** | Load and validate configuration from environment. Fails fast on missing `GOOGLE_API_KEY` or database URLs rather than at first use. All model identifiers are config, never literals (ADR-002). Supplies `USER_TIMEZONE` (ADR-011). |
| **TimeResolver** | Pure deterministic resolution of relative time descriptors against a message anchor, in the user's local zone (ADR-010, ADR-011). No model calls. Where most temporal bugs will be caught. |
| **MigrationRunner** | Apply pending numbered `.sql` migrations in a transaction; verify checksums of already-applied files; refuse to start on drift (ADR-004). |
| **SchemaDriftCheck** | Compare SQLAlchemy `Table` metadata against the live schema at startup and fail loudly on mismatch (ADR-009 boundary rule). |
| **DegradationPolicy** | Central policy for graceful degradation (NFR-06.5): what to do when retrieval fails, extraction times out, or Gemini is unreachable, and how to disclose it to the user. |
| **ObservabilityKit** | Structured logging with correlation IDs, plus timing spans across the retrieval and extraction pipelines. Needed because a 30s budget is impossible to tune blind. |
| **MemoryOperationLog** | Append-only audit of every memory mutation, satisfying the auditability requirement in specification section 12. |

---

## Component Count Summary

| Layer | Count |
|---|---|
| L1 API | 6 |
| L2 Orchestration | 7 |
| L3 Domain Services | 17 |
| L4 Ports | 5 |
| L5 Adapters | 5 |
| Cross-cutting | 6 |
| **Total** | **46** |
