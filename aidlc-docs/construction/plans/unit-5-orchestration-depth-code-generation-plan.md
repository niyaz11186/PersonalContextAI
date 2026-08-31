# Unit 5 — Orchestration Depth — Code Generation Plan

**Stage**: CONSTRUCTION → Code Generation, Part 1 (Planning)
**Status**: AWAITING APPROVAL — no code written yet
**This file is the single source of truth for Unit 5 code generation.** Part 2 executes it
step by step and marks each `[ ]` as `[x]` in the same interaction the work lands.

---

## 1. Unit context

**Goal** (`unit-of-work.md` §Unit 5): all five workflows exist, and extraction is off the
response path.

**Completion criterion** (verbatim):

> Correction workflow updates a memory and future responses respect it. Clarification workflow
> interrupts, survives a process restart, and resumes with intact state.

Both halves are falsifiable and neither is satisfiable by a test that only exercises the happy
path in one process. The second clause specifically requires killing and restarting the
application between interrupt and resume — an in-memory checkpointer passes a naive version of
this test and fails the real one, which is why the checkpointer work (Steps 4–5) is sequenced
before the ClarificationWorkflow rather than after it.

### What this unit retires

| Debt | Carried since | Retired by |
|---|---|---|
| **NFR-02.3 violation** — extraction runs synchronously inside the SSE response | Unit 1b | Steps 6, 12 |
| Memory-write orchestration living in the API layer (`api/conversation.py` lines ~140–200) | Unit 1b | Step 8 |
| `ConversationWorkflow` compiled with no checkpointer | Unit 1b | Step 5 |
| No `classify` node; `RetrievalService.budget_for()` takes no intent | Unit 1b | Steps 7, 11 |
| NFR-06.1 / NFR-06.5 disclosure text scattered as inline string literals in the router | Units 1b–4 | Step 3 |

### Dependencies (all satisfied)

`unit-of-work-dependency.md` U4→U5: workflows orchestrate retrieval; `ExtractionWorkflow` needs
Unit 2 extraction and Unit 3 conflict detection; `HistoricalAnalysisWorkflow` needs Unit 3
`TimelineService` and Unit 4 retrieval. Unit 4 is code-complete, so nothing here is blocked.

**Unit 5 does NOT include** (deferred by design, do not add): memory inspection/management HTTP
endpoints (Unit 6), `DeletionService` (Unit 6), explicit entity merge (Unit 6), import/export/
backup/reindex (Unit 7), `retrieval_diagnostics` persistence (Unit 7).

### Requirements traceability

| Requirement | Delivered by |
|---|---|
| NFR-02.3 — extraction must not block the response | Step 6 `ExtractionCoordinator`, Step 12 API rewiring |
| FR-02.1 — automatic extraction | Step 8 `ExtractionWorkflow` |
| FR-02.6 — recognise memory commands ("forget that", "that's wrong") | Step 7 `IntentRouter`. Execution of *forget* stays in Unit 6 |
| FR-05.1 — correct a mistaken memory | Step 9 `CorrectionWorkflow` |
| FR-04.5 / FR-05.5 — past reality vs past belief | Step 10 `HistoricalAnalysisWorkflow` routing split |
| FR-05.6 — surface contradictions, never resolve | Step 8 conflict branch |
| ADR-014 — ambiguity is never silently resolved | Step 11 `ClarificationWorkflow` |
| NFR-06.1 / NFR-06.5 — degrade with disclosure | Step 3 `DegradationPolicy` |
| ADR-006 — LangGraph confined to orchestration; interrupt/resume is its justification | Steps 5, 11 |
| ADR-008 — barrier: per-conversation, timeout, durable, idempotent | Steps 2, 6 |

---

## 2. Conditional stage assessment

| Stage | Decision |
|---|---|
| Functional Design | **SKIP** — `component-methods.md` already fixes every Unit 5 signature, and `services.md` gives node-by-node tables for all five workflows, the conflict-branch action table, and the correct-vs-supersede rule. Re-deriving them would restate, not decide. Same basis as Units 2–4. |
| NFR Requirements | **SKIP** — NFR-02.3 is the unit's completion condition; NFR-06.1/06.5 are already mapped to `DegradationPolicy`; ADR-008 fixes the barrier constraints. Tech stack settled in Unit 1. |
| NFR Design | **SKIP** — follows the above. |
| Infrastructure Design | **SKIP** — no new infrastructure. Checkpoints reuse the PostgreSQL already in the Compose stack (NFR-05.2); the queue is in-process per ADR-008's single-user scope. |
| Code Generation | **EXECUTE** — both parts. |

The open questions below are implementation choices, not design-stage questions, so they are
answered here rather than in a stage that would produce no new content.

---

## 3. Decisions required before Step 1

**Status: ANSWERED 2026-08-31 by explicit delegation.** The user replied "ok do it" to a message
that set out the recommended answer for each decision, delegating all six. Recorded as delegated
rather than user-authored, so that a later reader can see which choices carry the user's own
judgement and which carry mine.

Every one of these is a place where guessing wrong costs a rewrite rather than a tweak.

---

### D-1. Barrier placement on the request path

`services.md` Critical Path 1 rule 2 says *"the barrier is checked before append, not after"* —
otherwise message N+1 can be extracted before message N. Today the router appends first and
extracts inline. Enforcing the documented order means a rapid follow-up message waits for the
previous extraction before its own reply begins.

A) Follow `services.md` exactly: `await_barrier` → `append_message` → stream → `submit`. The
follow-up is *slow but correct* — the reply is generated with message N's facts already
committed, which is the whole point of ADR-008.

B) Append first (durability before any wait), then barrier, then stream. Message survives even if
the barrier times out; ordering guarantee preserved.

C) Do not block the reply at all; barrier only gates the next extraction. Fastest, but breaks the
"state a fact, immediately ask about it" case — the exact behaviour the core hypothesis rests on.

X) Other (please describe after [Answer]: tag below)

**Recommended: B.** It keeps `services.md`'s ordering guarantee while moving the durability
point earlier than the document assumes. A barrier timeout then cannot cost the user their
message — which A permits, since A waits before anything is persisted.

[Answer]: B (delegated)

---

### D-2. Background execution mechanism

ADR-008 requires durable state; the in-process lock is described as *"an optimization on top"*.

A) `asyncio.create_task` with a strong reference set, plus the durable `extraction_status` row.
No new infrastructure. Crash recovery is `recover_pending` at startup. Matches NFR-05.2 and the
single-user scope.

B) A single long-lived worker task draining an `asyncio.Queue`. Serialises extraction globally —
simpler to reason about, but violates ADR-008's *"the barrier is per-conversation, not global"*
since one conversation's slow extraction would delay every other.

C) External broker (Redis/Celery). Rejected by NFR-05.2 and the free-stack cost model.

X) Other (please describe after [Answer]: tag below)

**Recommended: A.** B is the tempting simplification and it is the one ADR-008 explicitly rules
out.

[Answer]: A (delegated) — **amended by the resiliency review**: A, but with a bounded task pool
rather than unbounded `create_task`. See RESILIENCY-10 in §8 and Step 6b.

---

### D-3. Clarification resume — how does the user's answer get back in?

`ClarificationWorkflow.resume(thread_id, user_answer)` needs a caller. There is no memory
management API until Unit 6.

A) Detect it in-band: `IntentRouter` recognises the next message in a conversation with an open
clarification as the answer, and routes it to `resume`. No new endpoint; the user just replies
naturally. Risk: misrouting a message that was not an answer.

B) Add a minimal `POST /conversations/{id}/clarifications/{thread_id}` endpoint in Unit 5, and
let Unit 6 build the richer surface on top. Explicit, directly testable, and makes the
restart-and-resume completion criterion exercisable over HTTP.

C) Both — endpoint for the deterministic test, in-band detection for real use.

D) Neither; `resume` stays unreachable until Unit 6. Would leave the completion criterion
unverifiable in this unit.

X) Other (please describe after [Answer]: tag below)

**Recommended: C.** The endpoint makes the restart-and-resume criterion testable over HTTP
without depending on classifier accuracy; in-band detection is what makes it usable.

[Answer]: C (delegated)

---

### D-4. Cost of `IntentRouter` on the response path

Classification is one extra model call before retrieval starts.

A) Small model (`gemini-3.5-flash-lite`, ~1.7 s) for every message. Consistent, but adds ~1.7 s
to every reply.

B) Deterministic prefilter first — obvious correction/forget/historical phrasings and very short
messages route without a model call; everything ambiguous escalates to the small model. Most
turns pay nothing.

C) Small model, but run concurrently with `load_history` so the latency overlaps. Combines with
A or B.

X) Other (please describe after [Answer]: tag below)

**Recommended: B + C.** Together they make classification close to free on the common path.

[Answer]: B + C (delegated)

---

### D-5. Barrier timeout behaviour

ADR-008: on timeout, *"proceed with a recorded degradation event and surface to the user that
recent context may be incomplete."* `PCA_EXTRACTION_BARRIER_TIMEOUT_SECONDS` already exists
(default 60).

A) Exactly as ADR-008 states: proceed, emit a `notices` entry on the SSE `done` event, record the
degradation. Abandoned extraction stays durable and is retried by `recover_pending`.

B) Fail the request with 503. Contradicts ADR-008 and loses the user's message.

C) Proceed silently. Violates NFR-06.5's disclosure clause.

X) Other (please describe after [Answer]: tag below)

**Recommended: A.** It is what ADR-008 already specifies.

[Answer]: A (delegated)

---

### D-6. Restructuring `workflow_checkpoints`

The table from `0001_foundation.sql` cannot hold a LangGraph 1.2 checkpoint: no `checkpoint_ns`
(namespaces collide on the primary key), no `metadata`, no pending-writes companion table, and
`state JSONB NOT NULL` is the wrong type for serde output, which is `(type: str, payload: bytes)`.
The table has never been written to — Unit 1b compiled the graph without a checkpointer.

A) `0004` restructures it: drop `state`, add `checkpoint_ns`/`metadata`/`type`/`payload`, repoint
the primary key, and add `workflow_checkpoint_writes`. Forward-only is preserved — `0001` is not
edited. Clean schema, no dead column.

B) Leave `state` in place and write a placeholder into it to satisfy `NOT NULL`. Keeps the diff
smaller at the cost of a permanently meaningless column.

C) Adopt `langgraph-checkpoint-postgres`. Rejected: it requires `psycopg`, adding a second
PostgreSQL driver and connection pool beside the existing asyncpg one, and it owns its own table
layout.

X) Other (please describe after [Answer]: tag below)

**Recommended: A.** The table is empty, so restructuring costs nothing now and B's dead column
is permanent.

[Answer]: A (delegated)

---

## 4. Code location

Per `aidlc-state.md`: application code at the workspace root, documentation in `aidlc-docs/`
only. Layer-first structure (Units Generation decision D4) — a boundary violation shows up as a
wrong import path.

Boundary rules that constrain this unit specifically:

- **Rule 2** — only `pca.orchestration` may import `langgraph`.
- **Rule 3** — no `sqlalchemy` in L3 services; they depend on repository ports.
- **C-25** — domain services depend on repository ports, never on `RelationalStorePort`.

The checkpointer is the one component that would violate both at once (it needs LangGraph types
*and* SQL), so it is deliberately split: `CheckpointStorePort` + SQLAlchemy adapter carry no
langgraph import; `PostgresCheckpointSaver` in `orchestration/` carries no sqlalchemy import.

---

## 5. Steps

### Step 1 — Schema `migrations/0004_extraction_status.sql`
- [x] `extraction_status` table: `episode_id` PK (idempotency by construction — a retry cannot
      double-write), `conversation_id`, `state`, `attempts`, `submitted_at`, `started_at`,
      `finished_at`, `error`, `updated_at`; CHECK constraint on `state`
- [x] Partial index on `(conversation_id)` `WHERE state IN ('pending','running')` — the barrier's
      only hot query
- [x] Index on `(state, submitted_at)` for `recover_pending`
- [x] Restructure `workflow_checkpoints` per D-6: drop `state`, add `checkpoint_ns` (default
      `''`), `metadata JSONB`, `type TEXT`, `payload BYTEA`; primary key becomes
      `(thread_id, checkpoint_ns, checkpoint_id)`
- [x] New `workflow_checkpoint_writes` table for `put_writes`:
      `(thread_id, checkpoint_ns, checkpoint_id, task_id, idx)` PK, `channel`, `type`, `payload`
- [x] Header comment stating what the migration does and why, matching the `0003` house style
- [x] **Added during execution**: a `DO $$ ... RAISE EXCEPTION` guard that aborts the migration if
      `workflow_checkpoints` is not empty. ADR-004 gives no downgrade path and no backup exists
      until Unit 7, so `DROP COLUMN` on a wrong premise would be unrecoverable. The premise is now
      asserted rather than trusted — directly prompted by the RESILIENCY-04 finding in §8
- [x] **Added during execution**: `workflow.workflow` relaxed to nullable. LangGraph creates
      checkpoints for nested graphs whose config we do not author, and failing a checkpoint write
      over a missing cosmetic label would trade durable state for a column nobody reads

### Step 2 — Table metadata and the extraction-status port
- [x] Declare `extraction_status` and `workflow_checkpoint_writes` in
      `src/pca/adapters/postgres/tables.py`; update the `workflow_checkpoints` declaration to
      match Step 1 (`SchemaDriftCheck` compares declared columns against live and fails startup
      on a mismatch, so these must agree)
- [x] `ExtractionStatusRepositoryPort` in `src/pca/ports/repositories.py` — domain types only:
      `claim`, `mark_running`, `mark_finished`, `get`, `in_flight_for_conversation`,
      `recoverable`, `count_by_state`
- [x] `CheckpointStorePort` in `src/pca/ports/checkpoints.py` — plain `bytes`/`str` in and out,
      **no langgraph import**. Given its own module rather than added to `ports/store.py`, which
      documents itself as the relational-store seam
- [x] `src/pca/adapters/postgres/extraction_status_repository.py`
- [x] `src/pca/adapters/postgres/checkpoint_repository.py`

### Step 3 — `DegradationPolicy` (L3, pure)
- [x] `src/pca/services/degradation.py`
- [x] `Degradation` domain type in `src/pca/domain/orchestration.py` carrying **both** the
      fallback action and the user-facing disclosure text, so NFR-06.5's "with disclosure"
      clause cannot be dropped by a caller that only reads the action
- [x] `on_retrieval_failure`, `on_extraction_timeout`, `on_provider_unavailable`
- [x] **Added**: `on_memory_write_failure` and `on_graph_unavailable`. Both are real degraded
      paths already present in the router and the episode service; leaving them out would have
      left half the degradation surface as inline literals, defeating the point of the module
- [ ] Replace the inline disclosure strings currently hardcoded in `api/conversation.py` with
      calls into this policy — deferred to Step 12, where the router is rewired anyway

### Step 4 — Orchestration domain types (L0)
- [x] `src/pca/domain/orchestration.py`: `RoutingDecision`, `BarrierResult`, `ExtractionRecord`,
      `ExtractionOutcome`, `Degradation`, `CorrectionRequest`, `AmbiguityContext`,
      `ClarificationOutcome`, `HistoricalQuery`
- [x] New enums in `src/pca/domain/enums.py`: `Intent`, `ExtractionState`, `DegradationAction`,
      `ClarificationStatus`
- [x] **Pulled forward from its planned position**: the Step 2 port signatures speak these types,
      so writing the port first would have meant either `Any` placeholders or a second pass
- [x] `Degradation.__post_init__` rejects empty disclosure text. NFR-06.5's "with disclosure"
      clause is the half most easily dropped by a caller that reads only the action, so it is
      enforced at construction rather than trusted to every call site (C-34)
- [x] `src/pca/domain/errors.py` — `ExtractionTimeout` already existed; added
      `ClarificationNotFound` (D-3 = C means the resume endpoint exists and needs it)

### Step 5 — `PostgresCheckpointSaver` (L2)
- [x] `src/pca/orchestration/checkpointer.py` subclassing `BaseCheckpointSaver`
- [x] Implement `aget_tuple`, `alist`, `aput`, `aput_writes`, `adelete_thread` over
      `CheckpointStorePort`. Verified against the installed `langgraph==1.2.11`:
      `__abstractmethods__` is `None`, so the sync surface may be left to the base class
- [x] Sync methods raise `NotImplementedError` with a message naming the async equivalent — the
      application is async throughout, and a silently-wrong sync path is worse than a loud one
- [x] Serialisation through `self.serde.dumps_typed` / `loads_typed`, stored as
      `(type TEXT, payload BYTEA)`
- [x] **Semantics taken from the `InMemorySaver` reference source, not documentation**: special
      channels take fixed negative indices from `WRITES_IDX_MAP`; ordinary writes are
      insert-if-absent while special writes overwrite. Both were wrong in the first draft of the
      adapter, which upserted everything — a resumed task re-emits its writes, so that would have
      replaced interrupt-time values with replay-time ones
- [x] `metadata` stored as JSONB rather than a blob so `alist(filter=…)` is a SQL predicate that
      composes with `LIMIT`
- [x] Deliberate simplification recorded: the whole checkpoint is one blob including
      `channel_values`. `InMemorySaver` splits them into versioned blobs to avoid rewriting
      unchanged values — an optimisation whose benefit scales with graph size, against doubling
      the ways a resume can come back partially wrong
- [x] `tests/fakes/checkpoints.py` and `tests/unit/test_checkpointer.py` (9 tests) written here
      rather than deferred to Step 15, because the restart-and-resume criterion is the riskiest
      thing in the unit and everything after this depends on it holding

### Step 6 — `ExtractionCoordinator` (L3)
- [x] `src/pca/services/extraction_coordinator.py`
- [x] `submit(episode_id, conversation_id)` — durable status row written **before** the task is
      spawned. Reversing that order is how a crash between the two loses the episode with no
      record it was ever queued
- [x] `await_barrier(conversation_id, timeout)` → `BarrierResult`, per D-1 and D-5
- [x] `recover_pending()` — re-queue rows left `running` by a crash
- [x] In-process `asyncio.Lock` per conversation as an optimisation over the durable row, never
      as a substitute for it (ADR-008). **Implemented as a per-conversation task set rather than
      a lock**: the barrier's question is "has this conversation's work finished", which
      `asyncio.wait` over the owning tasks answers directly, whereas a lock would have needed a
      parallel structure to track what it was guarding
- [x] `drain(timeout)` for graceful shutdown; `quiesce()`/`resume()` pair that Unit 7's backup
      will call
- [x] Idempotency: `claim` is a conditional insert on `episode_id`, so a duplicate submit is a
      no-op at the database rather than a race in the process
- [x] **Bounded task pool** (RESILIENCY-10 bulkhead)
- [x] **Per-extraction wall-clock timeout** distinct from the barrier timeout
- [x] Strong references held for spawned tasks. asyncio keeps only a weak reference to a running
      task, so one not stored anywhere can be collected mid-flight — extraction would simply stop,
      leaving a `running` row nobody retries until restart
- [x] `ABANDONED` kept distinct from `FAILED`, and included in `recoverable()`. Collapsing them
      would either retry genuine failures forever or discard work that was merely slow
- [x] `tests/fakes/extraction_status.py` and `tests/unit/test_extraction_coordinator.py`
      (14 tests), pulled forward from Step 15

### Step 6b — Provider bulkhead and explicit timeouts (RESILIENCY-10, RESILIENCY-09)

Added by the resiliency review. Not optional work invented by the extension — `services.md`
§Concurrency Model already specifies *"Gemini rate limits | Bounded concurrency semaphore in
`GeminiProviderAdapter`, with backoff"*. The backoff was built in Unit 1a; **the semaphore never
was.** Unit 5 is the point at which that matters, because until now every model call was on the
request path and therefore naturally serialised by one user typing. Background extraction removes
that accidental limit.

- [x] `asyncio.Semaphore` in `GeminiProviderAdapter` bounding concurrent calls, size from settings
- [x] Explicit timeout on every Gemini call. `_with_retry` previously retried on a timeout marker
      in the exception text but never imposed one, so a hung call waited on the SDK's default
      forever — an unbounded wait, which RESILIENCY-10 forbids outright
- [x] Timeout counts as retryable. Without that branch an explicit timeout would be strictly
      *worse* than none, failing calls the existing backoff would have recovered
- [x] Semaphore acquired per attempt and released before the backoff sleep. Holding a slot across
      an 8 s sleep would starve the bulkhead it exists to protect
- [x] `stream()` bounds establishing the stream, not consuming it. A long stream is legitimate;
      one that never yields a first chunk is the unbounded wait
- [x] Explicit timeout on `GraphitiMemoryAdapter` — `_guard()` applied to all seven call sites
      (`add_episode`, five searches, `rerank`, node lookup). The adapter previously had no
      timeout of any kind
- [x] Timeouts translate to `MemoryGraphUnavailable`, which callers already degrade on (ADR-005)
- [x] New settings: `PCA_MAX_CONCURRENT_LLM_CALLS`, `PCA_MAX_CONCURRENT_EXTRACTIONS`,
      `PCA_LLM_TIMEOUT_SECONDS`, `PCA_GRAPH_TIMEOUT_SECONDS`, `PCA_EXTRACTION_TIMEOUT_SECONDS`
- [x] Wired into `composition.py` immediately rather than deferred to Step 13 — an unwired
      setting is exactly the "specified but never built" failure this step exists to fix
- [ ] Document the Gemini free-tier request/minute limits in `SETUP.md` and the per-turn call
      budget that Unit 5 implies (RESILIENCY-09 service quota awareness)

### Step 7 — `IntentRouter` (L2)
- [x] `src/pca/orchestration/intent_router.py`
- [x] `classify(message, conversation_id) -> RoutingDecision` per D-4: deterministic prefilter
      first, escalating to the small model only for what survives it
- [x] Low confidence routes to clarification rather than guessing (`unit-of-work.md` §Routing)
- [x] FR-02.6 command recognition: *forget that*, *that's wrong*, *actually…* — classify only;
      *forget* execution stays in Unit 6
- [x] `has_open_clarification` short-circuit — the D-3 in-band half. Checked before the prefilter,
      because an answer can look like any other intent: "no, the one from work" would otherwise
      match the correction pattern
- [x] Prefilter tuned conservatively in one direction. Escalating an obvious message wastes a
      call; matching ordinary conversation routes a real question into a workflow that cannot
      answer it. Patterns anchor at the start, and FORGET additionally requires a short message
- [x] Classifier failure defaults to CONVERSE, not CLARIFY. Defaulting to clarification would
      interrogate the user on every turn while the provider is unwell — worse than the behaviour
      of the previous four units
- [x] `tests/unit/test_intent_router.py` (18 tests). **Caught a real defect**: the alternation
      group `(i|it|she|he|they)` had no trailing `\b`, so it matched the "i" inside "is" and
      "Actually is a word I overuse" prefiltered as a correction. Every group now ends in `\b`

### Step 8 — `ExtractionWorkflow` (L2)
- [ ] `src/pca/orchestration/extraction_workflow.py`, nodes per `services.md` Workflow 2:
      load episode → extract → resolve entities → retrieve related → detect conflicts → branch →
      commit → record belief → log operation
- [ ] Conflict branch actions exactly as tabulated: `AGREEMENT` reinforce, `REFINEMENT` attach,
      `TEMPORAL_CHANGE` supersede with `effective_from`, `CONTRADICTION` commit as uncertain and
      flag for surfacing — **never** pick a winner (FR-05.6)
- [ ] Idempotent by `episode_id` (ADR-008), enforced through `extraction_status`
- [ ] **Move**, not copy, the memory-write logic out of `api/conversation.py`. Leaving a second
      copy behind is how the two drift and a fix lands in only one of them

### Step 9 — `CorrectionWorkflow` (L2)
- [ ] `src/pca/orchestration/correction_workflow.py`, nodes per `services.md` Workflow 3
- [ ] The correct-vs-supersede decision (C-26): *"that's not what I said"* → `correct`;
      *"she moved in March"* → `supersede`. When the signal is weak the workflow **confirms via
      interrupt** rather than inferring — choosing wrong corrupts the timeline in a way that is
      hard to detect later
- [ ] Interrupt when more than one memory is affected ("confirm scope" node)
- [ ] Calls existing `MemoryService.correct` / `.supersede` — no new write path

### Step 10 — `HistoricalAnalysisWorkflow` (L2)
- [ ] `src/pca/orchestration/historical_workflow.py`, nodes per `services.md` Workflow 4
- [ ] The routing split that is the point of this workflow: *"what was true"* →
      `TimelineService.state_at`; *"what did I think"* → `BeliefHistoryService.believed_at`.
      Getting this wrong produces a confidently wrong answer, so it is asserted directly
- [ ] Time-range resolution through `LLMProviderPort.structured`, then deterministic arithmetic
      (ADR-010 division of labour — the model parses, our code computes)

### Step 11 — `ClarificationWorkflow` (L2)
- [ ] `src/pca/orchestration/clarification_workflow.py`, nodes per `services.md` Workflow 5
- [ ] `run(ambiguity)` interrupts via `langgraph.types.interrupt`; `resume(thread_id, answer)`
      continues via `Command(resume=...)`
- [ ] Compiled with the Step 5 checkpointer — this is the one place ADR-006's LangGraph
      dependency actually earns its place
- [ ] Hard rule: **may only write memory after receiving a user answer.** It never resolves
      ambiguity on its own authority (ADR-014)
- [ ] Triggered by `CommitReceipt.needs_clarification`, which Unit 2 already sets and which is
      currently only turned into a passive notice string

### Step 12 — Rewire `ConversationWorkflow` and the API
- [ ] `src/pca/orchestration/conversation_workflow.py`: add the `classify` node; on retrieval
      failure apply `DegradationPolicy.on_retrieval_failure` and proceed with conversation-only
      context plus disclosure, rather than failing; attach the checkpointer; pass intent to
      `RetrievalService.budget_for(intent)`
- [ ] `src/pca/services/retrieval.py`: `budget_for` accepts an optional intent (the Unit 1b
      comment anticipates exactly this)
- [ ] `src/pca/api/conversation.py`: barrier per D-1, `submit` after the stream, inline
      extraction removed, disclosure notices sourced from `DegradationPolicy`
- [ ] Clarification endpoint per D-3
- [ ] Update the Unit 1b docstrings that promise this work — they currently say "Unit 5 adds…"
      and would otherwise become stale claims about a unit that has shipped

### Step 13 — Composition and lifecycle
- [ ] `src/pca/composition.py`: construct the checkpoint repository, checkpointer, coordinator,
      intent router, degradation policy and all four new workflows; extend `Container`
- [ ] `start()`: coordinator `recover_pending` replaces (or wraps) the bare
      `episodes.recover_pending`, so recovery restarts *extraction*, not just graph ingestion
- [ ] `stop()`: `drain` in-flight extraction before closing the store, otherwise shutdown races
      an open transaction

### Step 14 — Health
- [ ] `src/pca/api/health.py`: extraction backlog by state and in-flight count. A stuck
      coordinator is otherwise invisible — the API keeps returning 200 and replies look normal
      while memory silently stops accumulating

### Step 15 — Unit tests
- [ ] `tests/fakes/extraction_status.py`, `tests/fakes/checkpoints.py`
- [ ] `tests/unit/test_extraction_coordinator.py` — per-conversation isolation (conversation B
      is not delayed by A), timeout produces a disclosure rather than an exception, duplicate
      submit is a no-op, `recover_pending` re-queues crashed rows
- [ ] `tests/unit/test_intent_router.py` — low confidence routes to clarification; FR-02.6
      commands recognised
- [ ] `tests/unit/test_degradation_policy.py` — every `Degradation` carries non-empty disclosure
      text (the NFR-06.5 clause, asserted directly so it cannot be dropped)
- [ ] `tests/unit/test_checkpointer.py` — round-trip through the fake store; `alist` ordering;
      namespace isolation (the defect D-6 fixes, pinned so it cannot regress)
- [ ] `tests/unit/test_correction_workflow.py` — correct vs supersede chosen correctly, and
      interrupt on weak signal
- [ ] `tests/unit/test_historical_workflow.py` — "what was true" and "what did I think" reach
      different services and return different answers for the same date
- [ ] `tests/unit/test_resiliency_bounds.py` (RESILIENCY-10) — concurrent extraction never exceeds
      the configured bound; a hung provider call is cut off by the per-call timeout rather than
      waiting forever; a hung extraction releases its pool slot. Each of these passes trivially
      against unbounded code if asserted loosely, so the bound is asserted by observing peak
      concurrency, not by checking the semaphore exists

### Step 16 — Integration tests
- [ ] `tests/integration/test_orchestration_flow.py`
- [ ] **Completion criterion, half 1**: correction workflow updates a memory, and a subsequent
      retrieval returns the corrected value rather than the original
- [ ] **Completion criterion, half 2**: clarification interrupts; the checkpointer is then
      re-instantiated against the same store to simulate a process restart; `resume` continues
      with intact state. Asserting this *without* discarding the in-memory graph object would
      test nothing — the state would still be in the process
- [ ] NFR-02.3: the SSE `done` event arrives before extraction completes. This is the assertion
      that actually retires the Unit 1b exception; a test that only checks extraction eventually
      happens would pass against the current synchronous code
- [ ] Barrier ordering: message N+1's reply is generated after N's facts are committed

### Step 17 — Documentation
- [ ] `aidlc-docs/construction/unit-5-orchestration-depth/completion-summary.md` — matching the
      Unit 3/4 format: completion criterion, what was built, design decisions worth recording,
      bugs found in existing code
- [ ] Update `aidlc-docs/aidlc-state.md`: Unit 5 status, new test count, any new C-NN constraints
- [ ] Append to `aidlc-docs/audit.md`

---

## 6. Expected new constraints

To be confirmed during Part 2 and recorded in `aidlc-state.md`:

| # | Constraint |
|---|---|
| C-32 | The extraction barrier is per-conversation. No global lock — one conversation's slow extraction must never delay another (ADR-008). |
| C-33 | `ClarificationWorkflow` may write memory only after a user answer. It never resolves ambiguity on its own authority (ADR-014). |
| C-34 | Every `Degradation` carries disclosure text. A degraded path that returns no user-facing notice is a defect, not an optimisation (NFR-06.5). |
| C-35 | `extraction_status.episode_id` is the idempotency key. Extraction retries must be no-ops at the database, not races in the process. |

---

## 7. Scope

18 steps (17 planned, plus Step 6b added by the resiliency review). New source files ~14;
modified ~12; one migration; three new test modules plus two fakes. Expected suite: 314 to roughly
390.

**Risks**

- **Highest**: the restart-and-resume half of the completion criterion. It depends on the custom
  checkpointer being correct, which is why Steps 5 and 15 precede Step 11 rather than following
  it, and why the restart test discards the graph object rather than trusting in-process state.
- Moving extraction off the response path changes when memory becomes visible. The barrier is
  what keeps "state a fact, then immediately ask about it" working; if D-1 is answered C that
  guarantee is knowingly given up.
- `IntentRouter` adds a model call to every turn. D-4 governs the cost.

---

## 8. Resiliency Baseline compliance (extension enabled)

Evaluated 2026-08-31 against `extensions/resiliency/baseline/resiliency-baseline.md`. This should
have been done before §5 was written; it was not, and the omission is recorded in audit.md.

Scope note: the extension is evaluated *for this stage*. Unit 5 Code Generation produces
application code, one migration, and tests. Rules governing requirements-phase decisions, cloud
topology, or operations are marked **N/A (stage)** with the unit that owns them named, rather than
silently passed.

| Rule | Status | Basis |
|---|---|---|
| RESILIENCY-01 Critical workload identification | **Partial** | `components.md` and `unit-of-work-dependency.md` give full dependency maps. No explicit criticality classification or impact statement. Single-user personal workload; impact of unavailability is "the assistant is unusable", of data loss "personal history is gone" — the second is what ADR-005 and ADR-013 exist for. Recommend documenting in Unit 7. |
| RESILIENCY-02 RTO/RPO targets | **NON-COMPLIANT (inherited)** | The prescribed RTO/RPO question was never asked during Requirements. No target exists anywhere in `aidlc-docs/`. Does not affect Unit 5 code, but Unit 7's `BackupService` has nothing to design against. Question raised in §9. |
| RESILIENCY-03 Change management | **N/A (stage)** | Single-developer personal project, no organisational process. Question raised in §9 for the record. |
| RESILIENCY-04 Automated deployment and rollback | **N/A (stage)**, one real flag | Local Docker Compose, no CI (C-18). But option D — *database-aware rollback* — genuinely applies: ADR-004 migrations are forward-only, so there is **no schema rollback**. Migration 0004 restructures `workflow_checkpoints`; if it fails midway the recovery path is restore-from-backup, and no backup exists until Unit 7. Mitigated here only by the table being empty. Question raised in §9. |
| RESILIENCY-05 Monitoring and alerting | **Partial** | Structured logging via `structlog` exists (`observability/logging.py`) and is used consistently. No metrics collection, no dashboard. Traces N/A — single service. Unit 7 hardening. |
| RESILIENCY-06 Health checks | **Compliant** | `/health` already reports per-dependency status plus the ingestion backlog. Step 14 extends it with extraction backlog and in-flight count. Load-balancer integration and synthetic monitoring N/A — localhost-only by C-8. |
| RESILIENCY-07 Resiliency monitoring | **Partial** | Backlog is observable but nothing alarms on it. Acceptable for a single-user local deployment where the operator is the user; revisit in Unit 7 alongside backup-failure alerting. |
| RESILIENCY-08 Multi-zone / multi-region | **N/A** | Local single-machine Docker Compose by explicit user decision (C-10, no cloud). Genuinely not applicable, not deferred. |
| RESILIENCY-09 Auto-scaling and quotas | **Partial → addressed** | Auto-scaling N/A. **Service quota awareness applies and was missing**: Gemini free-tier rate limits are a live constraint this project has already hit. Unit 5 raises per-turn call count (IntentRouter) and adds concurrent background calls. Step 6b documents the quota and the per-turn budget. |
| RESILIENCY-10 Dependency isolation and circuit breaking | **WAS NON-COMPLIANT → addressed in Steps 6, 6b** | See below. This is the finding that justified the review. |
| RESILIENCY-11 DR strategy | **N/A (stage)** | Unit 7 owns it (ADR-013). Blocked on RESILIENCY-02. |
| RESILIENCY-12 Backup and replication | **N/A (stage)** | Unit 7 owns it. Note ADR-013 does not currently mention backup encryption at rest — flagged for Unit 7. |
| RESILIENCY-13 Failover and recovery procedures | **N/A (stage)** | Unit 7 / Operations. |
| RESILIENCY-14 Chaos and DR testing | **Partial** | No practice defined, but Step 16's restart-and-resume test *is* a fault-injection test — kill the process mid-interrupt, verify recovery. Worth naming as the first entry in a DR test catalogue. Question raised in §9. |
| RESILIENCY-15 Incident response | **N/A (stage)** | Single-user personal project, no on-call. Question raised in §9. |

### RESILIENCY-10 — the finding worth having

Three of the four sub-rules were breached by the plan as originally written, and two of them by
the existing codebase.

**Timeouts — "all external calls MUST have explicit timeouts; no unbounded waits."** Verified by
reading the adapters: `GeminiProviderAdapter._with_retry` retries when an exception message
contains `"timeout"`, but never sets one. `GraphitiMemoryAdapter` contains no timeout of any kind.
Every model and graph call in the system is currently an unbounded wait. Until now the retrieval
budget governor masked this on the read path; nothing masks it on the write path.

**Bulkheads.** The plan's D-2 answer was `asyncio.create_task` per extraction with no bound. A
burst of messages would spawn unbounded concurrent Gemini calls, and the failure mode is not
graceful: hitting the rate limit pushes *every* conversation's barrier into timeout simultaneously,
so one saturated dependency degrades the entire write path at once. `services.md` §Concurrency
Model already prescribes the fix — *"bounded concurrency semaphore in `GeminiProviderAdapter`"* —
and it was specified in Inception but never built. Unit 5 is where its absence starts to bite,
because background extraction removes the accidental serialisation that one user typing provided.

**Circuit breakers.** Still absent. Judged acceptable for now: with a single provider and no
fallback (C-11), a tripped breaker and a failed call produce the same user-visible outcome, and
the retry/backoff plus the new semaphore already prevent the hammering a breaker would stop.
Recorded as a deliberate decision rather than an oversight.

**Graceful degradation.** Compliant — Step 3's `DegradationPolicy` is exactly this rule.

### Blocking findings

None remaining for this stage. RESILIENCY-10 and the RESILIENCY-09 quota gap are resolved in the
plan (Steps 6, 6b, 15). RESILIENCY-02 is non-compliant but is a Requirements-phase artifact that
does not constrain Unit 5's code; it blocks Unit 7 and is raised in §9.

---

## 9. Outstanding resiliency questions (never asked during Inception)

These are prescribed by the extension and must be answered by the user, not chosen by the model.
None block Unit 5. All block Unit 7. Answer whenever convenient.

### Question 1: RTO/RPO goals and disaster recovery strategy (RESILIENCY-02)

A) RPO/RTO hours — Backup & Restore. Lowest cost. Restore from backup on failure.

B) RPO/RTO tens of minutes — Pilot Light.

C) RPO/RTO minutes — Warm Standby.

D) RPO/RTO near real-time — Multi-site Active/Active.

E) N/A — single machine is acceptable; no cross-region DR needed.

X) Other (please describe after [Answer]: tag below)

[Answer]:

### Question 2: Database-aware rollback (RESILIENCY-04)

ADR-004 migrations are forward-only, so a failed migration has no automated reversal.

A) Accept it — recovery is restore-from-backup once Unit 7 exists. Document the exposure.

B) Require every future migration to ship a tested reversal script.

C) Take a manual `pg_dump` before applying any migration, as a documented step.

X) Other (please describe after [Answer]: tag below)

[Answer]:

### Question 3: Resiliency testing approach (RESILIENCY-14)

A) Use an existing DR testing / game day practice — provide the reference.

B) No practice exists — propose a DR test scenario catalogue, seeded with Unit 5's
restart-and-resume test and Unit 7's wipe-Neo4j-and-rebuild test.

C) Defer to the Operations phase; capture scenarios now, execute later.

X) Other (please describe after [Answer]: tag below)

[Answer]:

### Question 4: Change management and incident response (RESILIENCY-03, RESILIENCY-15)

A) Exempt — single-developer personal project; document the exemption rationale.

B) Propose a lightweight process for both.

X) Other (please describe after [Answer]: tag below)

[Answer]:
