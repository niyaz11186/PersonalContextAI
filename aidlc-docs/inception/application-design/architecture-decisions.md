# Architecture Decision Records

Your original specification (section 20) asked for evidence-based architecture decisions and explicit evaluation of LangGraph, Graphiti/Neo4j, Mem0, and Letta. This document records those evaluations.

Each ADR states the decision, the evidence, the alternatives rejected, and the consequences.

---

## ADR-001: Long-Term Memory Layer — Graphiti

**Status**: Accepted (user directive)

### Decision

Use `graphiti-core` as the temporal knowledge graph memory layer.

### Evidence

Graphiti is purpose-built for the exact problem in this specification. Per the [Graphiti project description](https://pypi.org/project/graphiti-core/), it builds temporal context graphs that track how facts change over time and maintain provenance back to source data, targeting agents operating on evolving real-world data. Its stated capabilities map directly onto the requirements:

| Requirement | Graphiti capability |
|---|---|
| FR-04.2 valid-from / valid-to | Temporal edge metadata extracted and updated from input |
| FR-04.3 supersession | Smart graph updates revise existing entities against new context |
| FR-06.2 hybrid retrieval | Semantic + BM25 full-text + graph traversal with result fusion |
| FR-02.5 provenance | Episode-based ingestion retains source linkage |
| FR-03.4 entity merging | Schema consistency reuses existing node/edge types |

### Alternatives evaluated and rejected

| Alternative | Why rejected |
|---|---|
| **Mem0** | Optimizes for extraction and recall of user preferences/facts. Does not provide bi-temporal validity intervals or supersession-with-history as first-class primitives. FR-04.4 (preserve historical states) and FR-05.5 (belief history) would need to be built on top. |
| **Letta / MemGPT** | Focused on self-editing agent context windows and tiered memory paging. Its model is a rewritten agent state, which directly conflicts with NFR-05.5 and specification section 5 ("avoid relying on a single continuously rewritten summary as its sole memory"). |
| **Build memory primitives from scratch** | Would reinvent temporal graph invalidation, hybrid search fusion, and entity resolution. Specification section 11 explicitly warns against unnecessary reinvention. |
| **Plain vector store (pgvector / Chroma)** | Specification section 1 explicitly rejects "a conventional chatbot with a vector database attached." No temporal or relational reasoning. |

### Consequences

- Neo4j becomes a hard dependency (see ADR-003).
- Graphiti's ontology influences our entity/edge shape. Mitigated by ADR-005 (we keep our own domain model and treat Graphiti as an engine, not the system of record for conversations).
- Framework maturity is a risk. Mitigated by keeping the integration behind a narrow port interface so it can be swapped.

---

## ADR-002: LLM, Embedding, and Reranking Provider — Gemini Only

**Status**: Accepted (user directive C-2)

### Decision

Configure Graphiti with the full Gemini client trio. No OpenAI dependency anywhere in the stack.

```python
from graphiti_core.llm_client.gemini_client import GeminiClient, LLMConfig
from graphiti_core.embedder.gemini import GeminiEmbedder, GeminiEmbedderConfig
from graphiti_core.cross_encoder.gemini_reranker_client import GeminiRerankerClient
```

Install with the `google-genai` extra: `graphiti-core[google-genai]`.

### Evidence

Gemini is the only Graphiti provider path that requires **no OpenAI credential and makes no OpenAI API call**. Per the [Graphiti LLM configuration guide](https://help.getzep.com/graphiti/configuration/llm-configuration):

- Gemini supplies all three roles: LLM inference, embeddings, and cross-encoding/reranking.
- The Anthropic integration still requires an OpenAI API key for embeddings and reranking.
- The Groq integration likewise requires an OpenAI key for embeddings.
- Graphiti explicitly performs best with providers that support structured output, and names Gemini as one such provider. This matters because Graphiti's entity extraction relies on structured output; weaker models cause ingestion failures.

Content rephrased for compliance with licensing restrictions.

### Correction, 2026-08-22 — the `openai` package cannot be excluded

An earlier version of this ADR implied that choosing Gemini keeps the `openai`
library out of the environment entirely. That is **wrong**, and was found when a
privacy guard test asserting its absence failed.

`graphiti-core` declares a hard, non-optional dependency on `openai>=1.91.0`. The
package is therefore installed whenever Graphiti is, regardless of which provider
is configured.

What constraint C-2 actually delivers, and what is now enforced by test:

| Claim | Status |
|---|---|
| No OpenAI API key configured | Enforced — `tests/unit/test_privacy_guards.py` checks `.env`, `.env.example`, and the environment |
| No OpenAI call ever made | Enforced — no `import openai` anywhere in `src/`, checked by test |
| No `openai` package installed | **Not achievable.** Transitive dependency of `graphiti-core` |

Without a credential the library is inert, so the privacy property holds. But the
dependency-level claim was overstated and is corrected here rather than left to
mislead a future reader.

### Consequences

- Single `GOOGLE_API_KEY` secret to manage.
- Gemini's structured-output support de-risks the extraction pipeline (FR-02.1).
- The Ollama path remains open for future local operation (NFR-04, Q8 "maybe later"), because Graphiti supports it via `OpenAIGenericClient` against Ollama's OpenAI-compatible endpoint. Recorded as a future option, not MVP scope.
- Model tier selection is deferred to a design question (see plan Q4).

### Note on model identifiers

Graphiti's documented examples use `gemini-2.0-flash` and `embedding-001`. Gemini model names move quickly and newer embedding models exist. All model identifiers are configuration values, never hardcoded, so they can be updated without code changes. Actual identifiers were to be pinned and verified against a live API call during **Unit 1** rather than assumed from documentation.

*Corrected 2026-08-11: this originally said Unit 2. That was inconsistent with the unit plan, which places both the Gemini provider adapter and the Graphiti adapter in Unit 1. Verification must happen in Unit 1 or the skeleton cannot run.*

### VERIFIED 2026-08-22 — model pins resolved

The deferral paid off. **Every model identifier in Graphiti's published examples is dead.** Live model listing and testing against the project's own API key produced:

| Documented in Graphiti | Actual status |
|---|---|
| `gemini-2.0-flash` | Not offered. Entire `gemini-2.5-*` family returns 404 "no longer available to new users" |
| `embedding-001` | Not offered. Available embedding models are `gemini-embedding-001`, `gemini-embedding-2`, `gemini-embedding-2-preview` |

Selection was made on **measured structured-output latency**, not version number, because structured output is the decisive capability — both our extraction pipeline and Graphiti's internal entity extraction depend on it, and a model that writes fluent prose but returns malformed schemas fails at ingestion rather than visibly.

| Candidate | Structured output | Extraction correct | Verdict |
|---|---|---|---|
| `gemini-3.7-flash` | **186.1 s** | Yes | Rejected — 7x over the entire 25 s retrieval budget on one call |
| `gemini-3.6-flash` | 34.5 s | Yes | Rejected — still over budget |
| **`gemini-3.5-flash`** | **2.9 s** | Yes | **Selected** |
| `gemini-2.5-flash` | — | — | Retired (404) |

Picking the newest model would have been wrong by a factor of sixty. This is the concrete payoff of ADR-002's "verify, do not assume" clause.

**Final pins:**

| Role | Model | Evidence |
|---|---|---|
| LLM | `gemini-3.5-flash` | 2.9 s structured, correct extraction |
| Small / classifier | `gemini-3.5-flash-lite` | 1.7 s, classified test sample correctly. `gemini-3.1-flash-lite` mislabelled the same sample |
| Embeddings | `gemini-embedding-001` | 3.0 s, 3072 dimensions |
| Reranker (Graphiti cross-encoder) | `gemini-3.5-flash-lite` | Low-latency classification task |

**On the embedding choice.** `gemini-embedding-2` also works, at the same 3072 dimensions and roughly 3x faster. `gemini-embedding-001` was selected anyway: it is the model Graphiti is most likely exercised against, and changing embedding model later invalidates every stored vector and forces a full reindex. The faster alternative is recorded here for when ingestion volume, rather than compatibility risk, becomes the binding constraint.

**Also fixed during verification.** The Google GenAI SDK enables automatic function calling by default and warns on every call. We pass no tools, so it is now explicitly disabled in `GeminiProviderAdapter._config`.

---

## ADR-003: Graph Database — Neo4j 5.26+ Community Edition

**Status**: Accepted

### Decision

Neo4j Community Edition, pinned to 5.26 or higher, self-hosted via Docker Compose.

### Evidence

Graphiti requires Neo4j 5.26+ (or FalkorDB 1.1.2+) per its [quick start requirements](https://help.getzep.com/graphiti/getting-started/quick-start). Since ADR-001 selects Graphiti, the graph backend is largely determined. Community Edition is free, which satisfies your constraint that only LLM APIs incur cost.

This supersedes the preliminary "Neo4j CE" note in the execution plan, which omitted a version pin. An unpinned or older image would fail at runtime.

### Alternatives evaluated and rejected

| Alternative | Why rejected |
|---|---|
| **PostgreSQL-only (JSONB + recursive CTEs)** | Was a genuine contender for reducing operational surface, and I would recommend it if we were building memory ourselves. But it is incompatible with ADR-001. Choosing it means dropping Graphiti and building temporal graph primitives by hand. |
| **Neo4j Aura managed** | Data would leave your machine, violating NFR-01.1. Also has cost beyond LLM APIs. |
| **FalkorDB** | Supported by Graphiti and lighter weight, but Neo4j has substantially better tooling (Browser UI for inspecting the graph during development) and a larger body of documentation. For a project where you will need to visually debug memory correctness, the Browser is worth the extra resource cost. |

### Consequences

- Two databases to operate (Neo4j + PostgreSQL). Accepted because they serve genuinely different roles (see ADR-005).
- Neo4j Community lacks multi-database and hot backups. Irrelevant for single-user MVP; backup via offline dump is sufficient for FR-10.2.
- Docker Compose must pin the image tag explicitly, not `latest`.

---

## ADR-004: Application Database — PostgreSQL with Raw SQL Migrations

**Status**: Accepted (user directive C-3)

### Decision

PostgreSQL for relational application data. Schema managed by numbered, forward-only raw SQL migration files applied by a small in-process runner. No Alembic.

### Design

```text
migrations/
├── 0001_initial_schema.sql
├── 0002_add_import_sources.sql
└── ...
```

A `schema_migrations` table records which files have been applied. On startup the runner lists migration files, compares against applied versions, and executes pending ones in order inside a transaction. This is roughly 40 lines of code and has no external dependency.

### Rationale

You asked for raw SQL now with tooling added later. This design honors that while keeping the door open: the numbered-file convention is exactly what Alembic, Flyway, and sqlx-migrate all expect, so adopting a tool later means writing a config file, not rewriting migrations.

### Trade-off stated plainly

The cost of forward-only raw SQL is no automatic downgrade path and no schema autogeneration from models. For a single-user local project where you can drop and recreate the database during development, this is a reasonable trade. It becomes a real liability once there is data you cannot afford to lose, which is the point at which adding a tool is worth revisiting.

### Consequences

- SQLAlchemy may still be used as a query layer / connection pool, but not as the schema source of truth. Whether to use SQLAlchemy Core, an async driver directly, or both is deferred to plan Q2.
- Migration correctness is manual. Mitigated by the runner refusing to proceed if a checksum of an already-applied file changes.

---

## ADR-005: Storage Split — Graphiti Owns the Graph, PostgreSQL Owns Source Truth

**Status**: ACCEPTED (user answer: Q1 = A)

### Context

This is the most consequential open decision in the architecture, and it comes directly from your specification. Section 11 says to avoid reinvention "while ensuring that the application's domain model and historical integrity are not dictated by a generic memory framework." Sections 3 and 5 insist the original conversation is preserved as source material and never replaced by AI-generated summaries.

Those two statements together imply a boundary: Graphiti should not be the only place your history lives.

### Decision

A two-store split with PostgreSQL as the system of record:

| Store | Owns | Why |
|---|---|---|
| **PostgreSQL** | Conversations, messages, raw imported documents, extraction audit log, memory operation log, belief-history snapshots, provenance index | Immutable source material (FR-01.4, FR-02.5). Must survive a Graphiti version change, a re-index, or a decision to swap memory frameworks entirely. |
| **Neo4j via Graphiti** | Entities, relationships, temporal facts, episodes, embeddings, hybrid search indices | Derived, rebuildable projection. Optimized for retrieval, not for durability of truth. |

The critical property: **Neo4j is rebuildable from PostgreSQL.** If the graph becomes corrupt, or Graphiti's schema changes, or you want to re-extract with a better model, you replay episodes from PostgreSQL. Your actual history is never at risk from a memory-framework decision.

This directly serves NFR-05.3 (evolutionary architecture) and the risk register entry for framework immaturity.

### Alternative

Let Graphiti be the sole store. Simpler — one database, less sync logic, faster to build. But it makes your entire personal history hostage to one library's schema and correctness, and it means a re-extraction with an improved prompt is impossible because the source episodes only exist inside the graph. Given that specification section 5 is titled "Memory Integrity" and states raw source material must remain available, I recommend against this.

### Consequences

- Write path: persist to PostgreSQL first, then ingest into Graphiti. PostgreSQL commit is the durability point.
- A rebuild/reindex command becomes a first-class feature, not an afterthought.
- Some conceptual duplication between the two stores, which is deliberate and is the price of the safety property.
- An `episodes` table is required in PostgreSQL recording the exact payload sent to Graphiti, so replay is byte-faithful.
- Reindex must be idempotent and resumable, since replaying months of episodes through Gemini will take time and may hit rate limits.

---

## ADR-006: Agent Orchestration — LangGraph

**Status**: ACCEPTED (user answer: Q3 = A)

### Context

Your specification asked to evaluate LangGraph against simpler alternatives and explicitly said not to assume it is mandatory.

### Assessment

The requirements describe five workflows (FR-08.2) that are conditional, stateful, and multi-step. Two properties push toward a real orchestration library rather than hand-rolled async Python:

1. **Durable execution across a slow pipeline.** You accepted up to 30s latency (NFR-02.1) for retrieval plus extraction plus generation. A crash mid-pipeline must not corrupt memory state. LangGraph's checkpointer gives this directly, and can persist to the PostgreSQL instance already in the stack.
2. **Interrupt for clarification.** FR-05.6 and the "ambiguous memory" workflow require pausing to ask the user a question, then resuming. Hand-rolling resumable pauses is where bespoke orchestration usually turns into an accidental framework.

### Honest counter-argument

For five workflows, plain `async` functions with explicit state objects would work and would be easier to debug. LangGraph adds a dependency, a mental model, and a version-churn risk. If the workflows stay simple, it is over-engineering. The deciding factor is whether you want checkpointing and resumable interrupts, because those are the parts that are genuinely unpleasant to build yourself.

### Recommendation

Adopt LangGraph, but confine it to the orchestration layer only. Workflow nodes call into our own service interfaces and contain no business logic themselves, so the graph definition stays thin and replaceable. Do not let LangGraph types leak into the memory or retrieval layers.

---

## ADR-007: Provider Abstraction — Thin Internal Port, No LiteLLM

**Status**: Accepted

### Decision

Define our own narrow provider interface for application-level LLM calls. Do not add LiteLLM for the MVP.

### Rationale

NFR-04 requires provider independence, but with C-2 fixing Gemini as the sole provider, LiteLLM would add a dependency to solve a problem we do not currently have. A thin internal port gives us the seam for provider independence at near-zero cost, and LiteLLM can be dropped in behind that seam later if a second provider actually arrives.

Note that Graphiti has its own provider clients (ADR-002), so this port governs only our application's direct LLM calls, not Graphiti's internal ones. Keeping these separate avoids fighting the framework.

---

---

## ADR-008: Extraction Timing — Hybrid with Per-Conversation Write Barrier

**Status**: ACCEPTED (user answer: Q2 = C)

### Context

Two requirements pulled in opposite directions. NFR-02.3 says extraction must not block the conversational response. But the core product hypothesis requires that a fact stated now is retrievable later — and "later" includes the very next message.

### Decision

Respond immediately using existing memory. Run extraction as a background task. Before processing any new message in a conversation, wait for that conversation's pending extraction to complete.

```text
Message N   arrives -> barrier for conv X is clear -> respond -> spawn extraction task E(N)
Message N+1 arrives -> barrier for conv X held by E(N) -> await E(N) -> respond -> spawn E(N+1)
```

### Design constraints this imposes

- The barrier is **per-conversation**, not global. Extraction in one conversation must not delay another.
- The barrier must have a **timeout**. If extraction hangs on a Gemini call, the user must not be blocked forever. On timeout, proceed with a recorded degradation event (NFR-06.5) and surface to the user that recent context may be incomplete.
- Extraction state must be **durable**, not only an in-process lock. If the process dies mid-extraction, the pending episode must be recoverable on restart. An in-memory `asyncio` primitive alone is insufficient; extraction status lives in PostgreSQL and the in-process lock is an optimization on top.
- Extraction must be **idempotent**. A retried extraction of the same message must not double-write facts.

### Consequences

- Adds an `ExtractionCoordinator` component owning barrier and queue semantics.
- Perceived latency is low for the first message in a burst and higher for rapid follow-ups, which is the correct trade: rapid follow-ups are exactly when the just-stated fact matters.
- Fresh-read correctness is guaranteed at message granularity, not sub-message.

---

## ADR-009: Database Access — SQLAlchemy Core over asyncpg

**Status**: ACCEPTED (user answer: Q4 = B)

### Decision

SQLAlchemy Core (expression language, not ORM) for query construction, running on the `asyncpg` driver. Schema remains in numbered raw `.sql` migration files per ADR-004.

### Rationale

Core gives composable, parameterized query building without an ORM's identity map, lazy loading, or model-as-schema-source-of-truth. Composability matters here because hybrid retrieval builds queries conditionally — temporal filters, entity filters, and full-text predicates combine differently per request. Building those by string concatenation is where SQL injection and unreadable code both come from.

### Boundary rule

Table definitions may be declared as SQLAlchemy `Table` metadata objects **for query building only**. They are never the schema source of truth and `metadata.create_all()` is never called. The `.sql` files are authoritative. A startup check compares declared metadata against the live schema and fails loudly on drift, which recovers the main safety property lost by not using Alembic.

---

## Decision Summary

| ADR | Decision | Status |
|---|---|---|
| 001 | Graphiti for temporal memory | Accepted |
| 002 | Gemini for LLM, embeddings, reranking — no OpenAI | Accepted |
| 003 | Neo4j 5.26+ Community, self-hosted | Accepted |
| 004 | PostgreSQL with raw SQL migrations, no Alembic | Accepted |
| 005 | PostgreSQL as system of record, Neo4j rebuildable | Accepted |
| 006 | LangGraph confined to orchestration layer | Accepted |
| 007 | Thin provider port, no LiteLLM | Accepted |
| 008 | Hybrid extraction, per-conversation write barrier | Accepted |
| 009 | SQLAlchemy Core over asyncpg, raw SQL schema | Accepted |

All architecture decisions are now settled. No open questions block Units Generation.

---

## Deferred Decisions

These are deliberately not decided yet, to avoid premature commitment.

| Topic | Deferred to | Reason |
|---|---|---|
| Object storage (S3/MinIO vs local filesystem) | Infrastructure Design | MVP is local-only. NFR-05.2 forbids unnecessary infrastructure. A port with a filesystem adapter satisfies FR-01.6 and FR-10 without a MinIO container. |
| Exact Gemini model identifiers and tiers | **Unit 1** implementation (corrected 2026-08-11) | Model names change frequently. Will be verified against a live API call, not assumed from docs. Originally stated as Unit 2, which was wrong — Unit 1 requires a working `GeminiProviderAdapter` and `GraphitiMemoryAdapter`, so verification cannot wait. |
| Reranking strategy tuning | Unit 4 implementation | Requires real data to tune. Premature tuning without a corpus is guesswork. |
| Evaluation framework depth | Post-MVP (user deferred at requirements Q14) | User chose to revisit later. |
| Encryption-at-rest mechanism | Infrastructure Design | Options range from filesystem-level to column-level. Depends on deployment target, which is local for now. |

---
---

# Addendum — ADRs 010 to 017

Added following the design audit of 2026-08-11. These resolve the gaps found in that audit.

---

## ADR-010: Relative Time Resolution — LLM Parses, Code Computes

**Status**: Accepted

### Context

Personal context is saturated with relative time references: "last Tuesday", "three weeks ago", "before the wedding". Storing the literal phrase makes it unusable later; defaulting to ingestion time makes the timeline wrong. For a system whose entire value is temporal correctness, this is load-bearing.

### Decision

Split responsibility by what each side is actually good at.

| Stage | Owner | Output |
|---|---|---|
| Identify the phrase and its structure | Gemini, via structured output | Raw phrase + structured descriptor (e.g. `{direction: past, quantity: 3, unit: week}`) |
| Compute the date | `TimeResolver`, pure deterministic code | Resolved instant or range |

Every message carries an absolute capture timestamp — the **anchor**. All relative expressions in that message resolve against that anchor.

LLMs are reliable at spotting time expressions and unreliable at date arithmetic. Letting the model compute dates directly would introduce silent, unfalsifiable errors into the timeline.

### Three properties that make this correct rather than merely working

**Raw phrase is retained.** `TemporalExpression.raw_phrase` stores "last Tuesday" alongside the resolved date. Enables re-resolution and audit, consistent with the provenance principle.

**Granularity is explicit.** `INSTANT | DAY | WEEK | MONTH | QUARTER | YEAR | UNKNOWN`. "Last summer" is not a timestamp. Recording fake precision for a vague phrase is the most direct route to a confidently wrong timeline.

**Event-relative references are not forced into dates.** "Before the wedding" cannot be resolved arithmetically. Resolution order:

1. Clock-relative → `TimeResolver` arithmetic
2. Event-relative → look up the referenced event; resolve if found
3. Unresolvable → store as an **ordering constraint** (`Relationship` with type `BEFORE` / `AFTER`), leave the date null, granularity `UNKNOWN`

An unresolvable reference on a significant memory routes to the clarification workflow rather than being guessed.

### Consequences

- New pure component `TimeResolver` and new value type `TemporalExpression`.
- Extraction structured-output schema gains temporal descriptor fields.
- `TimeResolver` is fully unit-testable with no model calls, which is where most temporal bugs will be caught.

---

## ADR-011: Timezone — UTC Instant Plus Per-Record Zone

**Status**: Accepted with a recorded variance from the user's instruction

### The instruction and the variance

The instruction was to store timestamps in the user's timezone. Implemented literally, that loses correctness: local timestamps without an attached zone cannot be ordered reliably across DST transitions, and cannot be compared as instants.

### Decision

Store both:

| Field | Type | Purpose |
|---|---|---|
| `occurred_at` / `captured_at` | `timestamptz` (UTC instant) | Ordering, comparison, indexing |
| `zone` | IANA name, e.g. `Asia/Kolkata` | Reconstructing local wall-clock time |

Rendered in the user's timezone at the API boundary. A single `USER_TIMEZONE` configuration value supplies the default.

**The user-facing outcome is exactly what was asked for** — times are seen in the user's timezone. The variance is purely in storage representation.

### Why the zone is stored per record, not only in config

If the user travels or relocates, a single global timezone setting retroactively corrupts the interpretation of past records. Storing the zone active at capture time is what allows "last Tuesday" to resolve against the correct day boundary years later.

### Consequences

- PostgreSQL columns use `timestamptz`, never bare `timestamp`.
- `ClockPort.now()` returns timezone-aware UTC; a companion accessor supplies the active zone.
- Interacts directly with ADR-010: day-boundary arithmetic runs in the local zone, not UTC.

---

## ADR-012: Deletion Model — Three Distinct Modes

**Status**: Accepted

### Context

FR-05.4 requires deletion to preserve an audit trail. NFR-01.6 requires complete erasure on request. These cannot both be one operation.

### Decision

| Mode | Target | Effect |
|---|---|---|
| **Forget memory** | One memory | Logical delete. Source untouched. Remains in inspection, marked deleted. |
| **Delete source** | Conversation / document | Source tombstoned. Derived memories retracted with cause `SOURCE_DELETED`. Nothing hard-deleted. |
| **Erase** | Anything | Genuine destruction across both stores. Requires explicit confirmation. Leaves a content-free audit stub only. |

### The corroboration rule

When a source is deleted, a derived fact is retracted **only if that was its last supporting source**. If a fact has provenance from three conversations and one is deleted, the fact stays active and only that provenance link is dropped.

Getting this wrong in either direction is damaging: retracting corroborated facts silently loses knowledge, while keeping uncorroborated facts presents claims whose evidence no longer exists.

### Rationale for retract-rather-delete

A fact whose evidence has been removed should not be presented as current truth, since that is precisely the hallucinated-history failure mode FR-07.4 targets. But erasing it outright destroys the audit trail FR-05.4 requires. Retraction satisfies both: the fact stops influencing answers, and the record of it having existed survives.

### Consequences

- `DeletionService` gains a source-deletion path distinct from memory deletion.
- `BeliefChangeCause` gains `SOURCE_DELETED`.
- Erase is deliberately awkward to invoke; it is the only operation in the system that genuinely destroys history.

---

## ADR-013: Backup — PostgreSQL Only, Neo4j Always Rebuilt

**Status**: Accepted

### Context

The two stores are not transactional together (ADR-005 consequence), so a naive simultaneous backup can capture an inconsistent pair. Neo4j Community Edition additionally has no online backup, implying downtime.

### Decision

**Neo4j is never backed up.** Back up PostgreSQL only, via `pg_dump`. Restore means restoring PostgreSQL and then running `ReindexService.rebuild()`.

This is legitimate precisely because ADR-005 makes Neo4j hold nothing authoritative. Both problems dissolve: there is no cross-store consistency question, and no Neo4j downtime.

### Refinements

- **Quiesce writes during backup.** Pause `ExtractionCoordinator` so the episode log contains no in-flight gaps. Cheap for a single user.
- **Record model identifiers in the backup manifest.** The Gemini LLM and embedding model IDs used at capture time must be recorded, because a rebuild needs compatible models. This also closes the embedding-model-mismatch gap from the audit: embeddings produced by different models are not comparable, and without recorded model IDs the mismatch is undetectable.
- **Optional Neo4j dump as a fast-restore cache.** Permitted, but labelled a cache, never a backup. If stale or corrupt, discard and rebuild.

### Trade-off

Restore is slower than a two-store restore and spends Gemini API calls replaying episodes. Accepted: backups become small, fast, online, and single-artifact, and correctness no longer depends on two stores agreeing.

---

## ADR-014: Entity Resolution — Never Silently Merge

**Status**: Accepted

### Context

Extraction is fully automatic (FR-02.3), so there is no human in the loop when the system encounters "Sarah" and knows three of them. Merging two distinct people into one entity corrupts every future retrieval about either, and is hard to unwind after the fact.

### Decision

| Match confidence | Action |
|---|---|
| High | Link to the existing entity |
| Ambiguous | **Create a new provisional entity**; flag for clarification |
| No match | Create a new entity |

Merging is **always an explicit operation**, never a side effect of extraction. `EntityService.merge` records rather than destroys, so it is reversible via `MemoryOperationLog`.

### Rationale

The asymmetry matters. A duplicate entity is a visible, correctable annoyance. A wrongly merged entity is invisible corruption that silently contaminates answers about both people. Erring toward duplication is the strictly safer failure direction.

### Consequences

- `EntityMatch` carries a confidence score and a threshold governs the branch.
- Provisional entities are queryable so duplicates can be found and merged deliberately.
- Interacts with ADR-015: Graphiti performs its own internal entity consolidation, which we do not treat as authoritative.

---

## ADR-015: Graphiti Ownership Boundary

**Status**: Accepted — clarifies ADR-005 rather than introducing a new decision

### Context

Graphiti performs entity resolution and temporal edge invalidation internally as part of its design. Our domain layer independently defines `EntityService`, `MemoryService.supersede`, and `BeliefHistoryService`. Left unstated, this yields two temporal models that can disagree, and "what was true in March?" could return different answers depending on which store answered.

### Decision

> Graphiti's internal entity resolution and temporal invalidation are treated as **retrieval optimizations, not as truth.** PostgreSQL holds the authoritative record of what the system believes and why. Where the two disagree, PostgreSQL wins and `ReindexService.verify` reports the drift.

We do not attempt to suppress Graphiti's internal behavior. We simply never read truth from it.

### Supporting measure

Define **custom Graphiti entity types** (Person, Organization, Place, Project) rather than relying on generic extraction, so Graphiti's graph stays aligned with our domain model and its consolidation decisions are less likely to diverge.

### Consequences

- Answers to temporal and belief queries are served from PostgreSQL-backed services, not from graph edge metadata.
- The graph is queried for *finding* candidates; PostgreSQL is queried for *asserting* what is true.
- `verify` gains responsibility for reporting entity-level divergence, not just episode counts.

---

## ADR-016: Evaluation Seams — Three Hooks, No Harness

**Status**: Accepted

### Context

The evaluation framework was deferred by the user at requirements Q14. But the original specification named evaluation a core feature, and retrofitting testability into a system that lacks hooks requires refactoring rather than addition.

### Decision

Build no harness. Preserve three seams, two of which already exist for other reasons.

| Seam | Status | Enables |
|---|---|---|
| Injectable clock (`ClockPort`) | Already in design | Simulating months of elapsed time in seconds |
| Ingest with explicit timestamp | Already needed for markdown import (`stated_date`) | Constructing synthetic histories |
| Persistable `RetrievalDiagnostics` | Add an optional flag and a landing table | Offline scoring of retrieval without re-running conversations |

### Consequences

Marginal cost, since two of three are already required. Later it becomes possible to express "store fact, advance clock three months, ask related question, assert the fact was retrieved" without modifying production code.

---

## ADR-017: Salience Scoring

**Status**: Accepted; detail deferred to Functional Design (Unit 3)

### Context

FR-02.2 mandates aggressive extraction. At the stated usage of 50+ messages per day, aggressive extraction will accumulate large volumes of low-value detail. The resulting failure mode is not forgetting — it is **burying**, where retrieval precision degrades because signal is diluted by trivia. Precision is the property the core hypothesis depends on.

### Decision

Retain aggressive extraction. Attach a **salience score** to each extracted memory so retrieval ranking can weight durable, consequential information above transient detail.

Signals that raise salience: descriptions of people and relationships, decisions and their reasoning, commitments, state changes in ongoing situations, emotionally significant events.

Bias toward keeping. The purpose is weighting, not filtering — nothing is discarded for low salience.

### Why this is not an architectural decision

Salience is a field on a memory record plus a term in the ranking function. It changes no layer, component, or dependency. Recorded here so the intent is not lost; scoring detail belongs in Functional Design.

---

## Updated Decision Summary

| ADR | Decision | Status |
|---|---|---|
| 001 | Graphiti for temporal memory | Accepted |
| 002 | Gemini for LLM, embeddings, reranking | Accepted |
| 003 | Neo4j 5.26+ Community, self-hosted | Accepted |
| 004 | PostgreSQL, numbered raw SQL migrations | Accepted |
| 005 | PostgreSQL system of record, Neo4j rebuildable | Accepted |
| 006 | LangGraph confined to orchestration | Accepted |
| 007 | Thin provider port, no LiteLLM | Accepted |
| 008 | Hybrid extraction, durable per-conversation barrier | Accepted |
| 009 | SQLAlchemy Core over asyncpg | Accepted |
| 010 | Relative time: LLM parses, code computes | Accepted |
| 011 | UTC instant plus per-record IANA zone | Accepted (variance recorded) |
| 012 | Three deletion modes with corroboration rule | Accepted |
| 013 | Back up PostgreSQL only; rebuild Neo4j | Accepted |
| 014 | Never silently merge entities | Accepted |
| 015 | Graphiti internals are optimization, not truth | Accepted |
| 016 | Three evaluation seams, no harness | Accepted |
| 017 | Salience scoring; detail in Functional Design | Accepted |

---

## Retracted from Earlier Design

| Item | Reason |
|---|---|
| CI import linter for boundary rules 1, 2, 6 | User opted out. Boundary rules remain as review guidance, enforced by discipline. Rule 6 is effectively enforced by never adding the `openai` dependency. |

## Retained After Challenge

Both were raised as possible scope creep during the design audit and explicitly retained by the user.

| Item | Note |
|---|---|
| `SchemaDriftCheck` + migration checksums | Recovers the main safety property lost by not using Alembic |
| Expanded ADR-008 barrier semantics | Durable status rows, timeout, idempotency, `recover_pending` |
