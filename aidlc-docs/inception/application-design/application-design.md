# Application Design — Consolidated

Personal Context AI Assistant. Consolidates `architecture-decisions.md`, `components.md`, `component-methods.md`, `services.md`, and `component-dependency.md`.

---

## 1. Design Summary

A modular monolith in Python/FastAPI with six layers and a ports-and-adapters boundary. Two data stores with asymmetric roles: PostgreSQL holds authoritative source material, Neo4j holds a rebuildable temporal graph projection managed by Graphiti. Gemini serves every model role. LangGraph orchestrates five workflows and is confined to one layer.

**47 components** across 6 layers. **17 architecture decisions**, all settled.

> **Revised 2026-08-11** following a design audit. ADRs 010–017 were added, resolving relative-time resolution, timezone representation, deletion cascade, backup consistency, entity-resolution ambiguity, the Graphiti ownership boundary, evaluation seams, and salience. Two `requirements.md` self-contradictions were corrected. See the addenda in `architecture-decisions.md` and `component-methods.md`.

### The three decisions that shape everything else

1. **PostgreSQL is the system of record; Neo4j is derived** (ADR-005). Your history cannot be damaged by a memory-framework decision. Reindex is a feature, not a recovery hack.
2. **Two time axes, not one.** `TemporalValidity` (when it was true) is separate from `BeliefWindow` (when the system thought so). This is what makes "what was true in March?" and "what did I think in March?" different questions with different answers.
3. **Contradictions are surfaced, never resolved silently.** `ConflictDetectionService` classifies rather than picks a winner.

---

## 2. Layer Model

```text
L1 API           6 routers, HTTP/JSON + SSE
L2 Orchestration 5 workflows + intent router + checkpointer (LangGraph only lives here)
L3 Domain        17 services, depend on ports only
L4 Ports         5 abstract interfaces
L5 Adapters      Graphiti, Gemini, Postgres, filesystem, clock
L6 Infra         Neo4j 5.26+, PostgreSQL, filesystem, Gemini API
```

Full definitions in `components.md`. Enforceable boundary rules in `component-dependency.md` section "Boundary Rules to Enforce in Review" — three of the seven are mechanically checkable and belong in CI.

---

## 3. Architecture Decisions

| ADR | Decision |
|---|---|
| 001 | Graphiti for temporal memory. Mem0 rejected (no bi-temporal validity), Letta rejected (rewritten-state model conflicts with Memory Integrity) |
| 002 | Gemini for LLM, embeddings, and reranking. The only fully OpenAI-free path through Graphiti |
| 003 | Neo4j 5.26+ Community, self-hosted. Version pin is a hard Graphiti requirement |
| 004 | PostgreSQL with numbered raw SQL migrations, no Alembic |
| 005 | PostgreSQL as system of record, Neo4j rebuildable |
| 006 | LangGraph confined to orchestration |
| 007 | Thin provider port, no LiteLLM |
| 008 | Hybrid extraction with per-conversation write barrier |
| 009 | SQLAlchemy Core over asyncpg; `.sql` files are schema authority |
| 010 | Relative time — Gemini parses the phrase, deterministic code computes the date |
| 011 | UTC instant plus per-record IANA zone (variance from instruction, recorded) |
| 012 | Three deletion modes with a corroboration rule |
| 013 | Back up PostgreSQL only; Neo4j is always rebuilt |
| 014 | Never silently merge entities; ambiguity creates a provisional entity |
| 015 | Graphiti internals are a retrieval optimization, not truth |
| 016 | Three evaluation seams, no harness |
| 017 | Salience scoring; scoring detail in Functional Design |

Deferred decisions are recorded explicitly in `architecture-decisions.md` rather than left implicit: object storage backend, exact Gemini model identifiers, reranking tuning, evaluation harness depth, and encryption-at-rest mechanism.

### Two further decisions worth surfacing

**Relative time is where this product succeeds or fails** (ADR-010). Personal context is saturated with "last Tuesday" and "three weeks ago". The split — Gemini identifies the phrase, our own deterministic code does the arithmetic — exists because models are unreliable at date math and their errors there are silent and unfalsifiable. Granularity is modelled explicitly so a vague phrase never acquires fake precision.

**Graphiti does entity resolution and temporal invalidation of its own** (ADR-015). Unaddressed, that would have produced two disagreeing timelines. The resolution follows from ADR-005: the graph is queried to *find* candidates, PostgreSQL is queried to *assert* what is true.

---

## 4. Critical Paths

**Write**: barrier check → PostgreSQL commit (durability point) → respond → background extract. The user's words are durable before any model call.

**Read**: budget → four concurrent searches → fuse → seeded traversal → rerank → assemble → stream. An explicit stop condition satisfies "smallest useful set" rather than returning everything similar.

Sequence diagrams and the rules governing each path are in `services.md`.

---

## 5. Requirements Traceability

### Count correction

My earlier messages said "41 FRs and 28 NFRs". Recounting from `requirements.md`, the actual totals are **54 functional** and **36 non-functional** requirements across the same 10 and 7 groups. The group counts were right; the item counts were not. Corrected here because traceability completeness depends on the real numbers.

### Status legend

`Covered` — design addresses it. `Deferred` — intentionally addressed in a later stage, target named. `Partial` — design addresses it in part, gap named.

### Functional Requirements

| ID | Status | Design element |
|---|---|---|
| FR-01.1 | Covered | ConversationRouter |
| FR-01.2 | Covered | SSE + `LLMProviderPort.stream` + streaming workflow iterator |
| FR-01.3 | Covered | ConversationService + LangGraph thread state |
| FR-01.4 | Covered | Append-only `ConversationService`; no message update/delete method exists |
| FR-01.5 | Covered | L1 has no UI coupling; API-first by construction |
| FR-01.6 | Covered | ImportService, ImportRouter, ObjectStorePort |
| FR-02.1 | Covered | ExtractionService + ExtractionWorkflow |
| FR-02.2 | Covered | Aggressive extraction policy in ExtractionService |
| FR-02.3 | Covered | No confirmation gate anywhere in the write path |
| FR-02.4 | Covered | `Origin` enum on every record |
| FR-02.5 | Covered | ProvenanceService, `ProvenanceRef`, `episodes` table |
| FR-02.6 | Covered | IntentRouter + MemoryManagementRouter + CorrectionWorkflow |
| FR-02.7 | Covered | `Origin` is immutable; `MemoryService` exposes no promote operation |
| FR-03.1 | Covered | EntityService + Graphiti entity nodes |
| FR-03.2 | Covered | `Relationship` model + `MemoryGraphPort.traverse` |
| FR-03.3 | Covered | `Event.participant_entity_ids` |
| FR-03.4 | Covered | `EntityService.resolve` / `merge` |
| FR-03.5 | Covered | `EntityService.attribute_history` over temporally-scoped Facts |
| FR-04.1 | Covered | ClockPort; every record carries a UTC instant plus the IANA zone active at capture (ADR-011) |
| FR-04.2 | Covered | `TemporalValidity` |
| FR-04.3 | Covered | `MemoryService.supersede`, `Fact.superseded_by` |
| FR-04.4 | Covered | Supersession adds; no destructive update path exists |
| FR-04.5 | Covered | `TimelineService.state_at` |
| FR-04.6 | Covered | `TimelineService.diff` |
| FR-04.7 | Covered | `TimelineService.reconstruct` |
| FR-04.8 | Covered | BeliefHistoryService + `BeliefWindow` |
| FR-05.1 | Covered | CorrectionWorkflow + `MemoryService.correct` |
| FR-05.2 | Covered | BeliefHistoryService + MemoryOperationLog |
| FR-05.3 | Covered | `DeletionService.forget_memory`; source deletion is a separate mode (ADR-012) |
| FR-05.4 | Covered | Logical deletion + append-only operation log. Source deletion retracts rather than erases, with the corroboration rule (ADR-012) |
| FR-05.5 | Covered | `BeliefHistoryService.believed_at` |
| FR-05.6 | Covered | ConflictDetectionService + `ContextPackage.conflicts` |
| FR-06.1 | Covered | ConversationWorkflow retrieve node |
| FR-06.2 | Covered | Five `MemoryGraphPort` search methods, fused |
| FR-06.3 | Covered | `RetrievalBudgetGovernor.should_continue` |
| FR-06.4 | Covered | `MemoryGraphPort.rerank` via GeminiRerankerClient |
| FR-06.5 | Covered | Context ceiling in BudgetGovernor |
| FR-06.6 | Covered | Budget permits 30s |
| FR-07.1 | Covered | ContextAssemblyService → `ContextPackage` |
| FR-07.2 | Covered | Four structural fields, not labels |
| FR-07.3 | Covered | `ContextPackage` composition |
| FR-07.4 | Covered | `render` separated from `assemble`; source excerpts included |
| FR-08.1 | Covered | LangGraph multi-node workflows |
| FR-08.2 | Covered | All five workflows designed |
| FR-08.3 | Covered | Conditional edges + PostgreSQL checkpointer |
| FR-08.4 | Covered | ADR-006 records the evaluation and the counter-argument |
| FR-09.1 | Covered | MemoryInspectionRouter |
| FR-09.2 | Covered | EntityService |
| FR-09.3 | Covered | `ProvenanceService.chain` |
| FR-09.4 | Covered | TimelineService |
| FR-09.5 | Covered | No UI component in MVP scope |
| FR-10.1 | Covered | ExportService |
| FR-10.2 | Covered | BackupService, PostgreSQL-only per ADR-013; Neo4j rebuilt on restore |
| FR-10.3 | Covered | `ExportFormat` targets JSON/markdown |

**54/54 addressed.**

### Non-Functional Requirements

| ID | Status | Design element / gap |
|---|---|---|
| NFR-01.1 | Covered | All stores local; only Gemini egress |
| NFR-01.2 | Partial | Egress points are structurally isolated to `GeminiProviderAdapter` and `GraphitiMemoryAdapter`. A written data-egress inventory is still owed — **deliverable in Infrastructure Design** |
| NFR-01.3 | Deferred | Encryption at rest → Infrastructure Design (mechanism depends on deployment target) |
| NFR-01.4 | Covered | Google GenAI SDK uses TLS |
| NFR-01.5 | Covered | ConfigurationManager reads env; fails fast; no literals |
| NFR-01.6 | Covered | `DeletionService.erase` with confirmation (ADR-012), distinct from logical deletion |
| NFR-02.1 | Covered | RetrievalBudgetGovernor |
| NFR-02.2 | Covered | Per-conversation barrier; single-user load |
| NFR-02.3 | Covered | ADR-008 background extraction |
| NFR-02.4 | Partial | Graphiti indices + reindex support growth; **behavior under months of data is unverified until Build and Test** |
| NFR-03.1 | Deferred | Docker Compose authored in Infrastructure Design |
| NFR-03.2 | Covered | Neo4j CE, PostgreSQL, filesystem — all free |
| NFR-03.3 | Covered | Gemini is the only paid dependency |
| NFR-03.4 | Deferred | Single-command startup → Infrastructure Design |
| NFR-03.5 | Covered | Ports and local-only adapters keep cloud migration open |
| NFR-04.1 | Covered | LLMProviderPort |
| NFR-04.2 | Covered | Model IDs are configuration |
| NFR-04.3 | Covered | Gemini primary per C-2 |
| NFR-04.4 | Covered | Per-task model configuration |
| NFR-04.5 | Covered | Requirement text corrected 2026-08-11 to Gemini-only with a provider-neutral port. Previously contradicted constraint C-2 |
| NFR-05.1 | Covered | Modular monolith |
| NFR-05.2 | Covered | No broker, no Redis, no K8s, no MinIO. Durable queue is a table |
| NFR-05.3 | Covered | Ports isolate every replaceable dependency |
| NFR-05.4 | Covered | Conflict surfacing over silent resolution |
| NFR-05.5 | Covered | ADR-005; raw episodes retained |
| NFR-05.6 | Covered | ObservabilityKit + `RetrievalDiagnostics` travelling with results |
| NFR-06.1 | Covered | Retry/backoff + DegradationPolicy. Requirement text corrected 2026-08-11 to remove "fallback provider", which was unsatisfiable with one provider |
| NFR-06.2 | Covered | PostgreSQL commit precedes model calls |
| NFR-06.3 | Covered | Transaction boundaries table in `services.md` |
| NFR-06.4 | Covered | ObservabilityKit structured logging |
| NFR-06.5 | Covered | `Degradation` carries fallback **and** disclosure text, so the disclosure clause cannot be dropped |
| NFR-06.6 | Covered | HealthRouter with per-dependency checks |
| NFR-07.1 | Deferred | Linter configuration → Build and Test |
| NFR-07.2 | Deferred | Test coverage targets → Build and Test |
| NFR-07.3 | Partial | Python type hints throughout; **TypeScript is out of scope** since MVP has no frontend |
| NFR-07.4 | Covered | Seven enforceable boundary rules |

**36/36 addressed**: 30 Covered, 4 Deferred with named target stage, 2 Partial with named gap.

Improved from the first revision: NFR-04.5 and NFR-06.1 moved from Partial to Covered by correcting the requirement text, which had contradicted the user's own later constraints. Remaining Partial: NFR-01.2 (egress inventory owed in Infrastructure Design) and NFR-02.4 (scale unverified until Build and Test). Remaining deferred: NFR-01.3, NFR-03.1, NFR-03.4, NFR-07.1, NFR-07.2 — with NFR-07.3 now closed, since TypeScript is formally out of scope while there is no frontend.

MVP scope item 14 (evaluation of retrieval and memory correctness) is not built, per the user's deferral, but is no longer blocked: ADR-016 preserves three seams that make the harness addable without refactoring.

---

## 6. Known Gaps and Accepted Risks

Stated plainly rather than buried.

| Gap | Nature | Disposition |
|---|---|---|
| **No authentication** | The API is unauthenticated. Security extension was opted out (Q16) and deployment is local single-user | Accepted for MVP. Service must bind to localhost only. Exposing this to any network interface without adding auth would expose all personal data. Recorded as constraint C-8, an operational rule rather than a to-do |
| Encryption at rest undecided | NFR-01.3 | Deferred to Infrastructure Design |
| Data-egress inventory not written | NFR-01.2 | Deferred to Infrastructure Design |
| Scale behavior unproven | NFR-02.4 | Verified in Build and Test, not assumed |
| Single provider adapter | NFR-04.5 | Deliberate under C-2. Requirement text corrected 2026-08-11 |
| Two stores not transactional | ADR-005 consequence | Mitigated by `verify` + `rebuild`. Backup sidesteps it entirely (ADR-013) |
| Graphiti/LangGraph version churn | Framework maturity | Mitigated by boundary rules 1 and 2, enforced by review discipline (CI linter dropped at user direction) |
| Raw SQL has no downgrade path | ADR-004 consequence | Acceptable while the database is disposable; revisit when data becomes irreplaceable |
| API cost at stated usage volume | ~15–25 Gemini calls per message at 50+ messages/day | **Explicitly dismissed by the user** — get it working first, optimise later. Unmitigated by choice, not oversight |
| Long-conversation prompt growth | No compaction policy | **Explicitly deferred by the user.** NFR-05.5 forbids aggressive summarisation, so this needs real design when it arrives |
| Restore is slow and costs API calls | ADR-013 consequence | Accepted trade for online, single-artifact, consistent backups |
| AI interpretations not first-class | Spec §3 describes a richer concept than `Origin.AI_INFERRED` | Minor. FR-07.3's labelling requirement is met. Post-MVP |
| "Current State" not explicitly modelled | Spec §3 | Approximated by `TimelineService.state_at(now)`. Minor |

---

## 7. Readiness for Units Generation

All seventeen architecture decisions are settled. No open question blocks decomposition.

The preliminary seven-unit split in the execution plan survives this design, with dependency order confirmed by the layer graph:

| Unit | Contains | Depends on |
|---|---|---|
| 1 Foundation | Config, migrations, schema, PostgresStoreAdapter, ClockPort, TimeResolver, MigrationRunner, SchemaDriftCheck | — |
| 2 Provider | LLMProviderPort, GeminiProviderAdapter | 1 |
| 3 Memory Engine | MemoryGraphPort, GraphitiMemoryAdapter (custom entity types), MemoryService, EntityService (ADR-014 policy), ProvenanceService, BeliefHistoryService, ConflictDetectionService, ExtractionService (temporal descriptors, salience) | 1, 2 |
| 4 Retrieval | RetrievalService, BudgetGovernor, ContextAssemblyService, TimelineService | 3 |
| 5 Orchestration | Five workflows, IntentRouter, Checkpointer, ExtractionCoordinator, DegradationPolicy | 4 |
| 6 API | Six routers, SSE streaming | 5 |
| 7 Infrastructure | Docker Compose, health checks, backup/restore (ADR-013), reindex, export/import | 6 |

`TimeResolver` sits in Unit 1 rather than Unit 3 deliberately: it is pure, has no dependencies, and is the single highest-value thing to unit-test early. Temporal correctness bugs found in Unit 1 are cheap; the same bugs found after months of accumulated memory are not.

Refinement of this split is the Units Generation stage's job.
