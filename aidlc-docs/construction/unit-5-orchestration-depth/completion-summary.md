# Unit 5 — Orchestration Depth — Completion Summary

**Status**: CODE COMPLETE. 447 tests passing offline. Migration 0004 not yet applied.
**Date**: 2026-09-04

## Completion criterion

> Correction workflow updates a memory and future responses respect it. Clarification
> workflow interrupts, survives a process restart, and resumes with intact state.

Both halves are asserted in `tests/integration/test_orchestration_flow.py`, together
with the two properties that make this unit worth doing at all.

**Half 1 — a correction changes what retrieval returns.**
`test_a_correction_changes_what_retrieval_returns` and
`test_the_corrected_value_reaches_the_next_reply`. The database holding the corrected
value is not sufficient: if retrieval keeps returning the original, the assistant
agrees it was wrong and then repeats the mistake, which is worse than never having
accepted the correction. Both the retrieval result and the rendered context are checked.

**Half 2 — a clarification survives a restart.**
`test_a_clarification_survives_a_process_restart`. The workflow object is discarded and
rebuilt against the same checkpoint store, which is what a restarted process sees.
Resuming through the original object would prove nothing — the interrupt would still be
in memory.

**NFR-02.3 — the reply does not wait for extraction.**
`test_the_reply_completes_before_extraction_runs` asserts the SSE `done` event arrives
with the episode's facts *not yet committed*. This is the assertion that actually
retires the exception carried since Unit 1b; a test that merely checked extraction
eventually happens would pass against the old synchronous code.

**ADR-008 — the barrier restores ordering.**
`test_the_next_message_sees_the_previous_messages_facts`. Extraction is deferred, so
without the barrier the second turn would retrieve against an empty store.

The last two pull against each other, and that tension is the reason ADR-008 exists:
extraction must not delay the reply, yet a fact stated now must be retrievable on the
next message.

## What was built

| Area | Deliverable |
|---|---|
| Barrier | `ExtractionCoordinator` — durable status rows, per-conversation barrier, timeout with disclosure, idempotency, `recover_pending`, bounded concurrency |
| Checkpointing | `PostgresCheckpointSaver` over `workflow_checkpoints` / `workflow_checkpoint_writes` |
| Routing | `IntentRouter` with a deterministic prefilter and a confidence threshold |
| Workflows | `ExtractionWorkflow`, `CorrectionWorkflow`, `HistoricalAnalysisWorkflow`, `ClarificationWorkflow` |
| Degradation | `DegradationPolicy` — every path pairs an action with the sentence the user must see |
| Resiliency | Provider semaphore and explicit timeouts on every Gemini and Graphiti call |
| Schema | `migrations/0004_extraction_status.sql` |
| Health | Extraction backlog by state, plus locally in-flight count |

## Design decisions worth recording

**The barrier is per-conversation, not global.** A global lock would let one
conversation's slow extraction delay every other. Recorded as C-32.

**`correct` versus `supersede` is confirmed, not inferred, when the signal is weak.**
`CorrectionWorkflow` interrupts rather than guessing, with a threshold (0.75) higher
than the router's. Routing wrongly costs a wasted turn; correcting on the wrong axis
costs the timeline, and the damage is undetectable until someone queries the wrong axis
months later.

**The clarification write is structurally unreachable without an answer.** The only
edge into `_apply` comes from `_ask`. That is a graph-shape guarantee rather than a
conditional a later editor could invert, which matters because ADR-014's failure mode —
a wrongly merged entity — is invisible corruption, unlike the duplicate it avoids.

**Unrecognised clarification answers abandon rather than guess.** Reaching that workflow
already means the system could not decide; treating "hmm, not sure" as a merge
instruction would defeat the point of stopping to ask.

**The historical workflow's axis routing is a pure function.** `_route` is a
`staticmethod` so the decision can be asserted without standing up three services. It
is the part that produces a *confidently wrong* answer when it breaks: answering the
world question with belief data asserts something known false, and answering the belief
question with world data erases the audit trail.

**Interpretation failure defaults to the world axis.** Being wrong toward world time is
less damaging — reporting what was true when asked what we thought is merely unhelpful,
whereas reporting a retracted belief as fact asserts a falsehood.

**Neither `ConversationWorkflow` nor `HistoricalAnalysisWorkflow` is checkpointed.**
This is a deliberate deviation from the plan, which called for attaching the
checkpointer to the conversation path. On inspection that is cost without a reader:
neither graph has an interrupt, so there is nothing to resume, and a checkpoint per
conversation turn would be durable writes nothing reads. `ClarificationWorkflow` is
where ADR-006's LangGraph dependency earns its place. If a later unit introduces a
mid-conversation interrupt, attaching it is a one-line change.

**PostgreSQL still does not degrade.** `ConversationWorkflow._retrieve` catches
retrieval failures and proceeds with disclosure, but re-raises
`SourceOfRecordUnavailable`. C-22 exists because an answer assembled without the system
of record would come from Neo4j alone, which ADR-015 designates non-authoritative — a
confidently wrong answer rather than a missing one.

## The design issue this unit surfaced

Moving extraction off the response path **silently dropped two things the system is
required to surface**: contradictions (FR-05.6, "surface the conflict, never pick a
winner") and entity ambiguity (ADR-014). Both are discovered during extraction, which
now finishes after the reply has already been sent. Nothing was left to report them.

That is a requirement traded away for latency, and it would have shipped unnoticed —
the notices simply stopped appearing.

Fixed by deferring rather than dropping: `ExtractionCoordinator` holds findings per
conversation and the API drains them after the barrier, which is the first moment the
extraction that produced them is guaranteed complete. **They now arrive one turn late.**
That delay is the honest consequence of ADR-008, and it is asserted directly in
`test_deferred_findings_reach_the_user_on_the_following_turn`.

The store is in-process, so notices are lost on restart. Acceptable because they are
advisory: the provisional entity and both conflicting facts are durably stored, and
Unit 6's inspection API surfaces them directly.

## Bugs found in existing code

**`graph.aget_state()` never returns `None`.** For an unknown thread LangGraph 1.2
returns a `StateSnapshot` with `values={}`, `next=()`, and `created_at=None` — which is
*truthy*. `CorrectionWorkflow.resume` guarded with `if await
self._graph.aget_state(config) is None:`, so the guard never fired. Resuming a bogus
thread restarted the graph from empty state and raised a bare `KeyError` from a node
reading `state["request"]` instead of `MemoryNotFound`.

Found while building `ClarificationWorkflow`, which had the same guard copied from
`CorrectionWorkflow`. Fixed in both, using `snapshot is None or snapshot.created_at is
None`, verified against the installed package rather than assumed. Regression test added
to `test_correction_workflow.py`.

**The RESILIENCY-10 fix was shipped untested.** The provider semaphore and the explicit
timeouts on every Gemini and Graphiti call landed in Step 6b with no coverage, which the
plan itself flagged as repeating the original mistake in a different place — the
semaphore was specified during Inception and never built, and staying unverified is how
that happened. `tests/unit/test_resiliency_bounds.py` (17 tests) now asserts:

- observed **peak** concurrency, not the existence of a semaphore
- that the bound *saturates*, so accidental serialisation cannot masquerade as compliance
- that raising the bound raises observed concurrency, proving the assertions are not vacuous
- a hung call is cut off, and a timeout **is retryable** (without that branch an explicit
  timeout would be strictly worse than none)
- the slot is released *before* the backoff sleep, timed against a deliberately long
  backoff so it does not flake under load
- `stream()` bounds establishing the stream, not consuming it
- every Graphiti search path is guarded and surfaces `MemoryGraphUnavailable`

## Test-harness findings

**Integration tests would race the background task.** Two doubles now exist, for
different reasons: `InlineExtractionCoordinator` runs extraction during `submit` so
end-state assertions stay deterministic, and `DeferredExtractionCoordinator` separates
submit from settle so the NFR-02.3 assertion is meaningful rather than vacuous.
Concurrency, timeouts, and per-conversation isolation are covered against the *real*
coordinator in unit tests, where time is controllable — asserting them through
`TestClient` would mean sleeping on wall clock and hoping.

**`test_temporal_flow.send()` created a new conversation per call**, so per-conversation
notices never carried over. Added `new_conversation` / `send_to`.

**A corrected fact's source excerpt shows the pre-correction wording.** Not a bug:
`MemoryService.correct` copies the original's provenance to the replacement, so the
excerpt points at the same utterance. The section is headed "what the user actually
said", and the fact states what is now believed, so the two are coherent. It is the
reason the assertion in `test_the_corrected_value_reaches_the_next_reply` is scoped to
the epistemic fact buckets rather than the whole prompt — a global assertion would
demand the system falsify its own transcript.

## Test-count change

380 → 447.

| File | Tests |
|---|---|
| `tests/unit/test_resiliency_bounds.py` | 17 |
| `tests/unit/test_historical_workflow.py` | 13 |
| `tests/unit/test_clarification_workflow.py` | 16 |
| `tests/unit/test_conversation_degradation.py` | 12 |
| `tests/integration/test_orchestration_flow.py` | 8 |
| `test_correction_workflow.py` (regression) | +1 |

## Not done in this unit

- **`SETUP.md` Gemini free-tier quota documentation** (RESILIENCY-09). The per-turn call
  budget is now materially higher — routing, extraction, conflict classification, and
  reranking — and that should be written down before someone hits the limit and reads it
  as a bug.
- **The four §9 resiliency questions** remain unanswered. All are scoped to Unit 7
  (RTO/RPO and DR strategy, migration rollback policy, DR testing, change management).
- **`invalidate_edge` / `entity_divergence`** remain `NotImplementedError`. Nothing calls
  them; `ReindexService` in Unit 7 is the first caller.
- **Live verification.** Requires the Docker machine.

## Activation steps

1. Sync and restart. Startup applies migration 0004; expect
   `migrations_up_to_date count=4` and `schema_drift_check_passed tables=17`.
2. Send a message and watch for the ordering that retires NFR-02.3:

       {"event": "conversation_context_ready", ...}   <- reply built
       data: {"done": true, ...}                       <- user has their answer
       {"event": "extraction_completed", ...}          <- only now

   `extraction_completed` appearing *before* the `done` event means the coordinator is
   not actually deferring.

3. Confirm the barrier by sending two messages in the same conversation and checking the
   second reply reflects the first message's facts.
4. Check the new health entry:

       curl -s http://127.0.0.1:8000/health | python -m json.tool

   The `extraction` dependency should report `0 pending, 0 running`. A non-zero `failed`
   count means extractions are not retrying and needs investigation.

5. Verify the durable status rows:

       docker exec pca-postgres psql -U pca -d pca -c "SELECT state, count(*) FROM extraction_status GROUP BY state;"

6. Restart the process while an extraction is pending and confirm
   `extractions_requeued` appears at startup. Without it, an episode left `running` by a
   crash stays invisible to retrieval permanently.
