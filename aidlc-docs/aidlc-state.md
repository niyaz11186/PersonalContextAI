# AI-DLC State Tracking

## Project Information
- **Project Type**: Greenfield
- **Project Name**: Personal Context AI Assistant
- **Start Date**: 2026-08-11T12:55:00+05:30
- **Current Stage**: CONSTRUCTION - Unit 5 code complete (447 tests). Awaiting activation.
- **Next Stage**: Unit 5 live activation on the Docker machine (migration 0004), then Unit 6 (Management & Inspection). Unit 4 live activation still outstanding and can be verified in the same session.
- **Core hypothesis**: PROVEN 2026-08-24. A fact stated in one conversation was
  correctly recalled in a separate conversation without being repeated.
- **Known quality gap**: recall is partial. See Unit 1b activation notes in audit.md.

## Verified Model Pins (2026-08-22)

Selected on measured structured-output latency, not version number. All model
names in Graphiti's documentation were found dead.

| Role | Model | Latency |
|---|---|---|
| LLM | `gemini-3.5-flash` | 2.9 s structured |
| Small / classifier / reranker | `gemini-3.5-flash-lite` | 1.7 s |
| Embeddings | `gemini-embedding-001` | 3.0 s, 3072 dim |

Rejected: `gemini-3.7-flash` (186 s structured), `gemini-3.6-flash` (34.5 s),
all `gemini-2.5-*` (404, retired for new keys), `text-embedding-004` (nonexistent).

## Workspace State
- **Existing Code**: No (confirmed by user 2026-08-11)
- **Reverse Engineering Needed**: No
- **Note**: Stale residue exists from work the user reverted at project start — `app/**/__pycache__` (bytecode only, no source), empty `backend/`/`UI/`, `requirements.txt`, `api_test.py`, `key_test.py`, `old.zip`. User directed these be treated as out of scope. Greenfield classification confirmed by user authority. Prior-run logs deleted.
- **Workspace Root**: c:\Users\niyaz.ahamed.shaik\Documents\PersonalContextAI

## Code Location Rules
- **Application Code**: Workspace root (NEVER in aidlc-docs/)
- **Documentation**: aidlc-docs/ only
- **Structure patterns**: See code-generation.md Critical Rules

## Extension Configuration
| Extension | Enabled | Decided At |
|---|---|---|
| Security Baseline | No | Requirements Analysis |
| Resiliency Baseline | Yes | Requirements Analysis |
| Property-Based Testing | No | Requirements Analysis |

**Enforcement note (2026-08-31)**: The Resiliency Baseline was enabled but its rules file was
never loaded during Units 1a–4 — no stage before Unit 5 carries a resiliency compliance summary.
The first review, run at Unit 5 planning, found RESILIENCY-10 breached by existing code. Load
`extensions/resiliency/baseline/resiliency-baseline.md` at every subsequent stage.

**Unresolved**: Question 17 presented the extension as "directional best practices"; the extension
file declares its own rules "blocking by default". The user's answer began "Not sure" — a vague
marker that `common/overconfidence-prevention.md` requires be followed up, and never was.
Enforcement strength is therefore still undefined. Open resiliency questions are in §9 of the
Unit 5 plan.

## Stage Progress
### INCEPTION PHASE
- [x] Workspace Detection - Completed 2026-08-11
- [x] Requirements Analysis - Approved 2026-08-11
- [x] User Stories - EXCLUDED (user directive: permanently excluded, not deferred)
- [x] Workflow Planning - Completed 2026-08-11
- [x] Application Design - Completed 2026-08-11 (Awaiting Approval)
- [x] Units Generation - Completed 2026-08-11 (Awaiting Approval)

### CONSTRUCTION PHASE — 7 Units, Strictly Sequential
- [x] Unit 1a — Offline Foundation — COMPLETE 2026-08-22 (53 unit tests, 12/12 live checks)
- [x] Unit 1b — Skeleton Activation — **COMPLETE 2026-08-24.** Completion criterion met:
      cross-conversation recall verified live. 132 tests passing.
      Top risk register entry retired: Graphiti + Gemini + Neo4j compose as documented.
- [x] Unit 2 — Extraction Depth — **COMPLETE 2026-08-24.** Verified live: facts, salience
      categories, entities, and both time axes populated. 240 tests passing.
- [x] Unit 3 — Temporal Integrity — **CODE COMPLETE + migration 0003 applied live
      2026-08-30.** Commit path verified atomic against PostgreSQL; `belief_history`
      and `memory_operations` populating. Completion criterion met in tests.
      **Supersede/correct not yet exercised live** — no HTTP endpoint until Unit 6,
      so the only live trigger is automatic supersession via conflict detection.
- [~] Unit 4 — Retrieval Depth — CODE COMPLETE, 314 tests passing offline.
      Five strategies genuinely distinct (each an explicit Graphiti SearchConfig),
      RRF fusion, seeded traversal, capped cross-encoder rerank, governor with a real
      stop condition. `RetrievalResult.facts` now populated from PostgreSQL per
      ADR-015 — it was always empty through Units 1b–3. Pre-Unit-5 audit found and
      fixed Graphiti's empty-query gate silently disabling `search_temporal` and
      `traverse` (310 → 314 tests). Awaiting live activation.
- [~] Unit 5 — Orchestration Depth — CODE COMPLETE, awaiting live activation
      - [x] Conditional-stage assessment — Functional Design, NFR Requirements, NFR Design and
            Infrastructure Design all SKIPPED. Rationale in audit.md and in the plan §2.
      - [x] Code Generation Part 1 (Planning) — plan at
            `aidlc-docs/construction/plans/unit-5-orchestration-depth-code-generation-plan.md`.
            18 steps. D-1..D-6 answered by delegation 2026-08-31 (B, A, C, B+C, A, A).
            Pre-planning source check of `langgraph==1.2.11` found that the
            `workflow_checkpoints` table from migration 0001 cannot hold a LangGraph 1.2
            checkpoint — no `checkpoint_ns`, no `metadata`, no pending-writes table, and
            `state JSONB` is the wrong type for serde output. Migration 0004 restructures it.
            Resiliency Baseline review (revision 2) found RESILIENCY-10 non-compliant:
            no timeout on any Gemini or Graphiti call anywhere in the codebase, and the
            bounded-concurrency semaphore that `services.md` specifies for
            `GeminiProviderAdapter` was never built in Unit 1a. Remediated by Step 6b.
            No blocking findings remain.
      - [x] Code Generation Part 2 (Generation) — **CODE COMPLETE 2026-09-04, 447 tests.**
            Steps 1–17 done. Completion criterion asserted end to end in
            `tests/integration/test_orchestration_flow.py`: a correction changes what
            retrieval returns, and a clarification survives a process restart (workflow
            object discarded, rebuilt against the same checkpoint store).
            **NFR-02.3 retired** — the SSE `done` event is asserted to arrive with the
            episode's facts not yet committed, which would fail against the old
            synchronous code.
            Two bugs found in shipped code: `graph.aget_state()` never returns None (an
            unknown thread yields a truthy StateSnapshot with `created_at=None`), so
            `CorrectionWorkflow.resume` restarted the graph and raised KeyError instead of
            MemoryNotFound — fixed in both resume paths; and the RESILIENCY-10 fix from
            Step 6b had shipped with no test coverage at all, now 17 tests.
            One requirement was nearly traded away for latency: moving extraction off the
            response path silently dropped contradiction (FR-05.6) and ambiguity (ADR-014)
            notices, because both are discovered after the reply is sent. Now deferred one
            turn rather than lost.
            Deliberate plan deviation: the checkpointer is NOT attached to
            `ConversationWorkflow` — no interrupt on that path means durable writes with
            no reader. Reasoning in the completion summary.
            Migration 0004 not yet applied; awaiting live activation.
- [ ] Unit 6 — Management & Inspection
- [ ] Unit 7 — Lifecycle & Hardening
- [ ] Build and Test

## Decomposition Decisions (Units Generation)

| Question | Answer | Effect |
|---|---|---|
| Build shape | B — Walking skeleton first | U1 is a thin end-to-end slice; later units deepen |
| Deepen first | A — Extraction before retrieval | Irreversible errors before recoverable ones |
| Infrastructure timing | A — Minimal dev env in U1 | Compose with Postgres + Neo4j only; tooling in U7 |
| Directory structure | A — Layer-first | Boundary violations visible as wrong import paths |

## Key Decisions Log
| Decision | Choice | Rationale |
|----------|--------|-----------|
| User scope | Single-user, self-hosted | MVP is personal use only |
| Interaction modality | API-first (no UI for MVP) | Prove backend first, add UI later |
| Memory extraction | Fully automatic | Aggressive extraction to avoid missing info |
| Latency tolerance | Up to 30s acceptable | Accuracy over speed |
| Deployment | Local Docker Compose | Privacy-first, free infrastructure |
| LLM provider | Provider-agnostic, Gemini preferred | Cost-conscious with quality flexibility |
| Offline capability | Not required for MVP | Cloud APIs acceptable |
| Graph database | Neo4j CE (Docker) — pending architecture evaluation | Free, accurate, Graphiti-compatible |
| Memory correction model | Full temporal correction | Track belief history over time |
| Memory inspection | API endpoints only (no UI) | Defer UI to post-MVP |
| Import capability | Basic text/markdown | Sufficient for MVP |
| Cost model | Free stack + paid LLM APIs only | Personal project budget |

## Binding Constraints (User Directives)

These are hard constraints, not preferences. Do not revisit without explicit user instruction.

| # | Constraint | Recorded |
|---|-----------|----------|
| C-1 | User Stories stage is EXCLUDED from the workflow permanently | 2026-08-11 |
| C-2 | Graphiti MUST use Gemini for LLM, embeddings, and reranking. OpenAI is excluded. | 2026-08-11 |
| C-3 | No Alembic. Raw SQL migrations only for initial implementation. | 2026-08-11 |
| C-4 | Neo4j pinned to 5.26+ (Graphiti hard requirement) | 2026-08-11 |
| C-5 | PostgreSQL is system of record; Neo4j is rebuildable projection | 2026-08-11 |
| C-6 | LangGraph confined to orchestration layer only | 2026-08-11 |
| C-7 | SQLAlchemy Core only — never ORM, never `metadata.create_all()` | 2026-08-11 |
| C-8 | API is unauthenticated; MUST bind to localhost only | 2026-08-11 |
| C-9 | Streaming via SSE (not WebSocket). User-confirmed. | 2026-08-11 |
| C-10 | No cloud/object storage. Local filesystem only. | 2026-08-11 |
| C-11 | No LLM fallback provider. Gemini only. | 2026-08-11 |
| C-12 | API cost optimisation explicitly deferred by user | 2026-08-11 |
| C-13 | Long-conversation compaction explicitly deferred by user | 2026-08-11 |
| C-14 | Timestamps stored as UTC instant + per-record IANA zone; rendered in user timezone | 2026-08-11 |
| C-15 | Entity merging is NEVER automatic. Ambiguity creates a provisional entity. | 2026-08-11 |
| C-16 | Graphiti internals are a retrieval optimisation, never a source of truth | 2026-08-11 |
| C-17 | Neo4j is never backed up; always rebuilt from PostgreSQL | 2026-08-11 |
| C-18 | No CI import linter. Boundary rules enforced by review. | 2026-08-11 |
| C-19 | No container runtime installed; installation deferred. Unit 1b onward blocked. | 2026-08-11 |
| C-20 | Target Python 3.13. Exact dependency pins, values fixed at first install. | 2026-08-11 |
| C-21 | Root `requirements.txt`, `api_test.py`, `key_test.py`, `old.zip` are reverted-work residue — NOT sources of truth | 2026-08-11 |
| C-22 | PostgreSQL unavailable = fail request, no degradation (reads and writes) | 2026-08-11 |
| C-23 | Graphiti telemetry MUST stay disabled (`GRAPHITI_TELEMETRY_ENABLED=false`) | 2026-08-22 |
| C-24 | `openai` package is an unavoidable transitive dep of graphiti-core. C-2 is enforced as: no OpenAI key, no OpenAI import. | 2026-08-22 |
| C-25 | Domain services depend on repository ports, never on RelationalStorePort | 2026-08-22 |
| C-26 | `correct` and `supersede` are distinct operations on distinct time axes. Correct ends BELIEF; supersede ends WORLD validity. Never conflate. | 2026-08-25 |
| C-27 | `belief_history` and `memory_operations` are append-only. No update or delete method may be added to their repositories. | 2026-08-25 |
| C-28 | A memory commit is ONE transaction spanning memory rows, provenance, belief history, and the audit entry. | 2026-08-25 |
| C-29 | Graph search results are candidates only. Facts returned to the user MUST be resolved from PostgreSQL, never constructed from Graphiti's edge text (ADR-015). | 2026-08-30 |
| C-30 | Our `EntityId` is NOT Graphiti's node uuid — they come from independent extraction passes. Graph entity scoping is by NAME. | 2026-08-30 |
| C-31 | Cross-encoder reranking MUST be capped. `GeminiRerankerClient.rank` issues one API call per passage. | 2026-08-30 |
| C-36 | Findings discovered during background extraction (contradictions FR-05.6, ambiguity ADR-014) MUST be delivered, one turn late if necessary. Moving work off the response path may not silently drop a user-facing requirement. | 2026-09-04 |
| C-37 | `graph.aget_state()` never returns None. Thread existence MUST be checked via `snapshot.created_at is None`, never `snapshot is None`. | 2026-09-04 |
| C-38 | A workflow gets a checkpointer only if it can interrupt. Checkpointing a linear read path is durable writes with no reader. | 2026-09-04 |

## Architecture Decisions (all settled)

| ADR | Decision |
|---|---|
| 001 | Graphiti for temporal memory (Mem0/Letta rejected with reasons) |
| 002 | Gemini for LLM + embeddings + reranking |
| 003 | Neo4j 5.26+ Community, self-hosted |
| 004 | PostgreSQL, numbered raw SQL migrations |
| 005 | PostgreSQL system of record, Neo4j rebuildable |
| 006 | LangGraph confined to orchestration |
| 007 | Thin provider port, no LiteLLM |
| 008 | Hybrid extraction, per-conversation write barrier |
| 009 | SQLAlchemy Core over asyncpg |
| 010 | Relative time: Gemini parses, deterministic code computes |
| 011 | UTC instant + per-record IANA zone |
| 012 | Three deletion modes with corroboration rule |
| 013 | Back up PostgreSQL only; rebuild Neo4j |
| 014 | Never silently merge entities |
| 015 | Graphiti internals are optimisation, not truth |
| 016 | Three evaluation seams, no harness |
| 017 | Salience scoring; detail in Functional Design |
