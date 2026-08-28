# AI-DLC State Tracking

## Project Information
- **Project Type**: Greenfield
- **Project Name**: Personal Context AI Assistant
- **Start Date**: 2026-08-11T12:55:00+05:30
- **Current Stage**: CONSTRUCTION - Unit 3 code complete (269 tests). Awaiting activation.
- **Next Stage**: Unit 3 activation on the Docker machine (migration 0003), then Unit 4 (Retrieval Depth)
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
- [~] Unit 3 — Temporal Integrity — CODE COMPLETE, 269 tests passing offline.
      Completion criterion met in tests: supersession retains both states
      (`state_at(Feb)` = Pune, `state_at(now)` = Bangalore), and after a correction
      `believed_at` returns a DIFFERENT answer from `state_at` for the same date.
      Commit is now atomic — the Unit 2 half-written-episode failure is test-guarded.
      Migration 0003 not yet applied; awaiting activation.
- [ ] Unit 4 — Retrieval Depth
- [ ] Unit 4 — Retrieval Depth
- [ ] Unit 5 — Orchestration Depth
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
