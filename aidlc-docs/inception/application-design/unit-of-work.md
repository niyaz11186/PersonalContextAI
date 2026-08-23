# Units of Work

## Decomposition Shape

**Walking skeleton first, then deepen** (plan Q1 = B). Unit 1 is a thin vertical slice through all six layers with real infrastructure and naive implementations. Units 2 onward each deepen one capability.

**Extraction is deepened before retrieval** (plan Q2 = A). Extraction errors are irreversible — a fact stored with the wrong date or attributed to the wrong person corrupts the graph and may go unnoticed for months. Retrieval errors are recoverable at any time because the underlying data is intact.

**Minimal dev environment in Unit 1** (plan Q3 = A). Just enough Docker Compose to run Postgres and Neo4j. Operational tooling lands in Unit 7.

**Layer-first directory structure** (plan Q4 = A).

Seven units, with Unit 1 split into 1a and 1b. Strictly sequential — each depends on all predecessors.

**Infrastructure constraint (2026-08-11)**: no container runtime is installed on the development machine and installation is deferred. Unit 1a is buildable now; Unit 1b and everything after it are blocked until PostgreSQL and Neo4j can run.

---

## Dependency Pinning Strategy

Exact pins, not ranges. Framework churn in `graphiti-core` and `langgraph` is a named risk, and ranges would let a breaking minor release arrive silently between sessions.

**The pinned values are deliberately not written yet.** Version numbers will be fixed at the first successful install and recorded in `pyproject.toml` from what actually resolves and works. Inventing version numbers now would be asserting something unverified — the same class of error as the earlier workspace-detection mistake.

The existing `requirements.txt` in the workspace root is residue from reverted work and is **not** a source of truth. It uses ranges and includes `litellm`, which ADR-007 rejects.

Target runtime: **Python 3.13** (matches the existing venv; `graphiti-core` requires `>=3.10,<4`).

---

## Code Organization Strategy

Single deployable application, modular monolith (NFR-05.1). Layer-first tree so that a layering violation shows up as a visibly wrong import path.

```text
PersonalContextAI/
├── docker-compose.yml
├── .env.example                    # GOOGLE_API_KEY, USER_TIMEZONE, DB URLs
├── pyproject.toml
├── migrations/                     # raw SQL, numbered, forward-only (ADR-004)
│   ├── 0001_foundation.sql
│   ├── 0002_memory_model.sql
│   ├── 0003_temporal_integrity.sql
│   └── 0004_extraction_status.sql
├── src/pca/
│   ├── main.py                     # FastAPI app factory
│   ├── composition.py              # composition root: builds adapters, binds ports, injects
│   ├── domain/                     # L0: pure types. dataclasses, enums. ZERO dependencies.
│   ├── api/                        # L1: routers, request/response schemas, SSE
│   ├── orchestration/              # L2: LangGraph workflows. ONLY place langgraph is imported.
│   ├── services/                   # L3: domain services. Depend on ports only.
│   ├── ports/                      # L4: abstract interfaces. No third-party types.
│   ├── adapters/                   # L5: graphiti/, gemini/, postgres/, files/, clock/
│   ├── config/                     # ConfigurationManager, MigrationRunner, SchemaDriftCheck
│   └── observability/              # structured logging, correlation IDs, timing spans
├── tests/
│   ├── unit/
│   └── integration/
└── aidlc-docs/                     # documentation only, never application code
```

### Note on `domain/`

The six layers in Application Design describe *dependency direction*. The domain types (`Fact`, `Event`, `Entity`, `TemporalExpression`, enums) must be importable by every layer without creating a cycle, so they live in a dependency-free `domain/` package beneath all of them. This is an addition to the six-layer model, recorded here so it is not mistaken for a layering violation.

### Import rules by directory

| Directory | May import |
|---|---|
| `domain/` | stdlib only |
| `ports/` | `domain/` |
| `adapters/` | `domain/`, `ports/`, third-party SDKs |
| `services/` | `domain/`, `ports/`, other `services/` |
| `orchestration/` | `domain/`, `services/`, `langgraph` |
| `api/` | `domain/`, `orchestration/`, `services/`, `fastapi` |
| `composition.py` | everything |

`graphiti_core` appears only under `adapters/graphiti/`. `langgraph` appears only under `orchestration/`. `sqlalchemy` appears only under `adapters/postgres/`. The `openai` package appears nowhere (constraint C-2).

---

## Unit 1 — Walking Skeleton

**Split into 1a and 1b on 2026-08-11.** No container runtime is available on the development machine (verified: no Docker, docker-compose, or Podman installed) and installation is deferred by the user. PostgreSQL and Neo4j therefore cannot run yet.

Rather than leave a unit that cannot reach its completion criterion, Unit 1 is split along the line of what actually requires infrastructure. Gemini is a cloud API and needs no container; `TimeResolver` is pure. A substantial part of the skeleton is buildable and testable now.

---

### Unit 1a — Offline Foundation

**Goal**: everything buildable and unit-testable with zero running infrastructure.

**Blocked by**: nothing.

| Area | Deliverable |
|---|---|
| Project setup | `pyproject.toml` targeting **Python 3.13**, exact-pinned dependencies, package layout |
| Scaffolding | Full layer-first tree including `domain/`, plus `composition.py` skeleton |
| Domain | All pure types: `Fact`, `Event`, `Entity`, `Relationship`, `ProvenanceRef`, `TemporalValidity`, `BeliefWindow`, `TemporalExpression`, `RelativeDescriptor`, and every enum (`Origin`, `Confidence`, `Granularity`, `ResolutionMethod`, `BeliefChangeCause`) |
| Time | **`TimeResolver`** — pure, deterministic, exhaustively unit-tested. Highest-value component to build now |
| Ports | All five port interfaces defined: `ClockPort`, `RelationalStorePort`, `LLMProviderPort`, `MemoryGraphPort`, `ObjectStorePort` |
| Clock | `SystemClockAdapter` plus a scriptable test clock (ADR-016 seam) |
| Config | `ConfigurationManager` with fail-fast validation, `.env.example` |
| Gemini | `LLMProviderPort` + `GeminiProviderAdapter` — written, and verifiable against the live API once a key is supplied. Needs no container |
| SQL authored | `0001_foundation.sql` written but not applied |
| Compose authored | `docker-compose.yml` written but not run. Neo4j image tag pinned to 5.26+ |
| Observability | Structured logging, correlation IDs |
| Tests | Fake implementations of every port, so services are testable without infrastructure |

#### Completion criterion for 1a

`TimeResolver` passes an exhaustive test suite covering relative expressions, granularity, DST boundaries, and unresolvable cases. All domain types and ports importable with no dependency cycles. `GeminiProviderAdapter` returns a real completion against the live API. Nothing requires a database.

#### Note on Gemini verification

ADR-002 places model-identifier verification in Unit 1. That is doable here, but requires a `GOOGLE_API_KEY` supplied via `.env`. The key found in `key_test.py` is out of scope by user direction and is not used. Until a key is provided, the adapter is written and unit-tested against a fake, with live verification pending.

---

### Unit 1b — Skeleton Activation

**Goal**: `docker-compose up`, POST a message, receive a streamed response that demonstrably used something stored in an earlier conversation.

**Blocked by**: container runtime installation.

This is the part that retires the largest risk in the project — whether Graphiti, Gemini, and Neo4j actually compose as documented.

| Area | Deliverable |
|---|---|
| Infrastructure | Run the authored Compose stack; verify Neo4j reports 5.26+ |
| Schema | `MigrationRunner` applies `0001_foundation.sql`: `schema_migrations`, `conversations`, `messages`, `episodes`, `workflow_checkpoints` |
| Postgres | `PostgresStoreAdapter` — SQLAlchemy Core over asyncpg, integration-tested |
| Graph | `GraphitiMemoryAdapter` with `GeminiClient` + `GeminiEmbedder` + `GeminiRerankerClient`, exposing `add_episode` and `search_semantic` |
| Services | `ConversationService` (append-only); naive `ExtractionService`; naive `RetrievalService` (semantic only); naive `ContextAssemblyService` |
| Orchestration | `ConversationWorkflow` (LangGraph, four nodes) + `WorkflowCheckpointer` on PostgreSQL |
| API | `ConversationRouter` with SSE streaming; basic `HealthRouter` |

#### Deliberately absent from 1b

Write barrier, conflict detection, temporal resolution wired into extraction, belief history, entity resolution, hybrid retrieval, reranking, correction, deletion, import, export, backup, reindex.

**Extraction runs synchronously**, knowingly violating NFR-02.3. The barrier lands in Unit 5. Recorded so it is not mistaken for an oversight.

#### Completion criterion for 1b

State a fact in conversation A. Start conversation B. Ask a question requiring that fact. Receive a correct answer that used it.

---

### Consequence of the split

Unit 2 (Extraction Depth) depends on 1b, not 1a, because extraction needs a graph to write into. So the Docker dependency does not disappear — it moves. Work can proceed through 1a and then stops until a runtime exists.

`TimeResolver` being in 1a is fortunate rather than planned: the component where temporal errors are silent and permanent is also the one component that needs no infrastructure at all.

---

## Unit 2 — Extraction Depth

**Goal**: what gets captured is correct, well-structured, and temporally accurate.

### Contents

| Area | Deliverable |
|---|---|
| Extraction | Full `ExtractionService`: facts, events, entities, relationships, `Origin` tagging (user-stated vs AI-inferred) |
| Temporal | `TimeResolver` — pure, deterministic (ADR-010). `TemporalExpression`, `RelativeDescriptor`, `Granularity`, `ResolutionMethod`. Event-relative references become `BEFORE`/`AFTER` ordering constraints rather than fabricated dates |
| Timezone | UTC instant plus per-record IANA zone (ADR-011). Day-boundary arithmetic in local zone |
| Entities | `EntityService` with the ADR-014 policy: high confidence links, ambiguity creates a **provisional** entity, never a silent merge |
| Graphiti | Custom entity types — Person, Organization, Place, Project (ADR-015) |
| Salience | Scoring per ADR-017; weighting, not filtering |
| Provenance | `ProvenanceService`; `Fact.provenance` is a **list** to support the corroboration rule |
| Schema | `0002_memory_model.sql`: `entities`, `facts`, `events`, `relationships`, temporal columns, `salience`, `provenance_index` |

### Completion criterion

Feed a paragraph containing two same-named people and the phrase "last Tuesday". Verify: the date resolved to the correct day in the user's timezone, granularity is `DAY`, the raw phrase was retained, and no silent entity merge occurred.

`TimeResolver` is pure and must be unit-tested exhaustively here. Temporal bugs caught in Unit 2 are cheap; the same bugs found after months of accumulated memory are not.

---

## Unit 3 — Temporal Integrity and the Memory Write Path

**Goal**: the two time axes work, and history is never lost.

### Contents

| Area | Deliverable |
|---|---|
| Write path | Full `MemoryService`: `commit`, `correct`, `supersede`, `retract`. Correct and supersede are distinct operations with different effects on history |
| Belief | `BeliefHistoryService`, `BeliefWindow`, `BeliefChangeCause` |
| Conflicts | `ConflictDetectionService` — four-way classification. `CONTRADICTION` surfaces, never resolves |
| Timeline | `TimelineService`: `reconstruct`, `state_at`, `diff` |
| Audit | `MemoryOperationLog`, append-only |
| Transactions | Boundaries per `services.md`; PostgreSQL commit precedes graph ingestion |
| Schema | `0003_temporal_integrity.sql`: `belief_history`, `memory_operations`, supersession columns |

### Completion criterion

Assert "she lives in X". Later assert "she moved to Y in March". Verify both states are retained, `state_at(February)` returns X, `state_at(now)` returns Y. Then correct a mistaken fact and verify `believed_at` returns a **different** answer from `state_at` for the same date — proving the two axes are genuinely independent.

---

## Unit 4 — Retrieval Depth

**Goal**: retrieve the smallest useful set, not everything similar.

### Contents

| Area | Deliverable |
|---|---|
| Hybrid retrieval | All five strategies — semantic, full-text, entity-scoped, temporal, graph traversal — run concurrently and fused. Traversal seeded from fused results, never blind |
| Graph port | Remaining `MemoryGraphPort` search methods plus `rerank` via `GeminiRerankerClient` |
| Budget | `RetrievalBudgetGovernor` — explicit stop condition and context ceiling |
| Assembly | Full `ContextAssemblyService`: four-way structural split, conflicts, source excerpts, `render` separated from `assemble` |
| Diagnostics | `RetrievalDiagnostics` travelling with results; optional persistence behind a flag (ADR-016 seam) |

### Completion criterion

A question that requires history returns a small, relevant context package. Diagnostics show which strategies contributed and what the governor discarded.

---

## Unit 5 — Orchestration Depth

**Goal**: all five workflows, and extraction off the response path.

### Contents

| Area | Deliverable |
|---|---|
| Barrier | `ExtractionCoordinator` per ADR-008: durable status rows, per-conversation barrier, timeout, idempotency, `recover_pending`. **Satisfies NFR-02.3**, retiring the Unit 1 exception |
| Workflows | `ExtractionWorkflow` as a graph, `CorrectionWorkflow`, `HistoricalAnalysisWorkflow`, `ClarificationWorkflow` with interrupt and resume |
| Routing | `IntentRouter` with confidence; low confidence routes to clarification rather than guessing |
| Degradation | `DegradationPolicy` returning both fallback action and user-facing disclosure text |
| Schema | `0004_extraction_status.sql` |

### Completion criterion

Correction workflow updates a memory and future responses respect it. Clarification workflow interrupts, survives a process restart, and resumes with intact state.

---

## Unit 6 — Memory Management and Inspection API

**Goal**: you can see and control everything the system believes.

### Contents

| Area | Deliverable |
|---|---|
| Inspection | `MemoryInspectionRouter`: search memories, browse entities, provenance chains, timeline queries |
| Management | `MemoryManagementRouter`: correct, supersede, forget |
| Deletion | `DeletionService` three modes (ADR-012): `forget_memory`, `delete_source`, `erase`. **Corroboration rule** — retract only when the last supporting source is gone |
| Entities | Explicit reversible `merge`; `list_provisional` to surface duplicates |
| Provenance | `chain`, `source_excerpt` with surrounding context |

### Completion criterion

Create a fact supported by two conversations. Delete one. Verify the fact survives with one provenance link dropped. Delete the second. Verify the fact is now retracted, visible, and marked `SOURCE_DELETED`.

---

## Unit 7 — Data Lifecycle and Operational Hardening

**Goal**: your data is portable and recoverable.

### Contents

| Area | Deliverable |
|---|---|
| Import | `ImportService`, `ImportRouter` — text/markdown with `stated_date` so imported history does not collapse onto today |
| Export | `ExportService` — JSON/markdown |
| Backup | `BackupService` per ADR-013: **PostgreSQL only**. `BackupManifest` records LLM and embedding model IDs. Quiesces `ExtractionCoordinator` during backup |
| Reindex | `ReindexService`: `rebuild` (idempotent, resumable), `verify`, `entity_divergence` |
| API | `DataManagementRouter` |
| Startup | Full sequence: checksum verify, apply migrations, `SchemaDriftCheck`, Neo4j version gate, Graphiti index init, `recover_pending` |
| Health | Per-dependency checks for all infrastructure |
| Egress | Written data-egress inventory (NFR-01.2, owed since Application Design) |

### Completion criterion

Back up. Wipe Neo4j entirely. Restore PostgreSQL and rebuild the graph. Verify `verify()` reports parity and previously-working queries still return correct answers. This is the test that proves ADR-005 actually holds.

---

## Summary

| # | Unit | Retires | Blocked? |
|---|---|---|---|
| 1a | Offline Foundation | Temporal-resolution correctness | No — buildable now |
| 1b | Skeleton Activation | Stack integration risk | **Yes — needs container runtime** |
| 2 | Extraction Depth | Irreversible capture errors | Via 1b |
| 3 | Temporal Integrity | Two-time-axis correctness | Via 1b |
| 4 | Retrieval Depth | Context precision | Via 1b |
| 5 | Orchestration Depth | Workflow completeness, NFR-02.3 | Via 1b |
| 6 | Management and Inspection | User control and visibility | Via 1b |
| 7 | Lifecycle and Hardening | Data portability, ADR-005 validation | Via 1b |

**Only Unit 1a can proceed until a container runtime is installed.** Everything from 1b onward needs PostgreSQL and Neo4j running.
