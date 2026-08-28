# Unit 3 — Temporal Integrity — Completion Summary

**Status**: CODE COMPLETE. 269 tests passing offline. Migration 0003 not yet applied.
**Date**: 2026-08-25

## Completion criterion

Both halves are asserted as executable tests in
`tests/unit/test_temporal_integrity.py`.

**Part 1 — supersession retains both states.**
`test_supersession_retains_both_states_across_time`

    assert "Priya lives in Pune"            (January)
    supersede -> "Priya lives in Bangalore" (effective March)

    state_at(February) == ["Priya lives in Pune"]
    state_at(June)     == ["Priya lives in Bangalore"]

**Part 2 — the two axes diverge after a correction.**
`test_correction_makes_the_two_axes_diverge`

    January: "Priya works at Google"
    June:    correct -> "Priya works at Microsoft"

    state_at(February)    -> Microsoft   (the Google fact was never true)
    believed_at(February) -> Google      (that is what the system thought then)
    comparison.differs    -> True

Part 2 is the load-bearing test. A single-axis implementation cannot pass it: if both
methods read the same column they can never disagree, and the "two time axes" claim
would be decoration.

## The prerequisite that had to be fixed first

`MemoryService.commit` was **not transactional**. Confirmed by reading the code and
demonstrated live during Unit 2 activation: a commit wrote facts and entities, then
failed on relationships, leaving a half-written episode with no signal.

This blocked Unit 3 rather than merely being untidy. A supersession writes the
replacement fact, ends the original's world validity, appends two belief transitions,
and appends an audit row. If any of those lands without the others the timeline is
corrupt in a way no later read can detect — a fact marked superseded with no record of
why.

Now one transaction spans entity resolution, memory rows, provenance, belief history,
and the audit entry. Guarded by `test_a_failed_commit_leaves_no_trace`, which
reproduces the exact live failure, and `test_a_commit_uses_exactly_one_transaction`,
which asserts on transaction *identity* rather than a count — several independent
transactions would also satisfy "some transactions were opened".

## correct versus supersede

The distinction that makes this unit worth building. Conflating them silently corrupts
the timeline.

| | belief axis | world axis | why |
|---|---|---|---|
| `correct` | ENDS (`retracted_at`) | untouched | the record was wrong; the fact was never true, so there is no true period to preserve |
| `supersede` | CONTINUES | ENDS (`valid_to`) | the world changed; we still believe the old fact was true for its window |

If supersession retracted the old belief, "where did Priya live before Pune?" would
have no answer. If correction left world validity in place, the system would assert
that a fact it knows to be false was nonetheless true for a period.

Both directions are test-guarded:
`test_supersession_preserves_belief_and_ends_only_world_validity` and
`test_correction_ends_belief_and_leaves_world_validity_alone`.

## What was built

| Area | Deliverable |
|---|---|
| Schema | `migrations/0003_temporal_integrity.sql` — `belief_history`, `memory_operations`, `facts.corrected_from`, `facts.supersedes` |
| Domain | `domain/history.py` — `BeliefTransition`, `MemoryOperation`, `TimelineDiff`, outcomes; `OperationKind` enum; `MemoryNotFound` |
| Ports | `BeliefRepositoryPort`, `OperationLogRepositoryPort`; `tx` on every write method; `TransactionManagerPort` |
| Adapters | `adapters/postgres/history_repositories.py`, `adapters/postgres/scope.py` |
| Services | `BeliefHistoryService`, `MemoryOperationLog`, `TimelineService`, `ConflictDetectionService`; `MemoryService.correct/supersede/retract` |
| Wiring | `composition.py` container fields; conflict detection on the request path in `api/conversation.py` |

## Design decisions worth recording

**`belief_history.statement` is snapshotted, not a foreign key.** A correction rewrites
`facts.statement`, so a reference would resolve to the corrected text and the earlier
belief would be unrecoverable — defeating the table's only purpose.

**`TransactionManagerPort` rather than widening to `RelationalStorePort`.** C-25 says
domain services depend on repository ports, never on the store. `MemoryService` needs
exactly one capability — open a transaction — so it got a protocol with exactly that.
`PostgresStoreAdapter` satisfies it structurally, so there is no extra adapter to keep
in sync.

**`tx` parameter rather than a UnitOfWork object.** Rejected the bundle as more
machinery for the same guarantee: services would depend on the bundle rather than on
the one or two repositories they actually use. `Transaction` is this project's own
protocol, so no storage library leaks into L3.

**`end_belief` and `end_validity` are separate methods.** A single setter taking a
column name would make writing the wrong axis easy, which is the mistake that silently
corrupts a timeline.

**`TimelineDiff` has three buckets, not two.** "Stopped being true" and "we were wrong"
are different events. Reporting a correction as a change in the world would tell the
user their situation changed when the record was merely fixed.
Guarded by `test_diff_separates_ceased_from_corrected`.

**Corrections in `diff` come from the belief axis.** Found during implementation:
`state_at` excludes retracted facts, so a corrected fact is absent from *both*
endpoints and comparing world state at two instants detects nothing. This required
adding `BeliefRepositoryPort.transitions_between`. The first implementation was wrong
and the test caught it.

**TEMPORAL_CHANGE must not collapse into CONTRADICTION.** "She moved to Bangalore" does
not contradict "she lives in Pune" — it ends it. Treating every change as a
contradiction would ask the user to arbitrate every ordinary life event until they
stopped reading the prompts. Collapsing the other way is worse: silently superseding a
genuine contradiction picks a winner, which FR-05.6 forbids. There is deliberately no
`resolve` method on `ConflictDetectionService`.

**Unrecognised classifications default to CONTRADICTION.** An extra question is
recoverable; treating an unparseable response as "these agree" would let a real
contradiction through unnoticed.

**Conflict detection failure does not fail the commit.** An undetected conflict is a
missed opportunity to ask. A failed commit loses the memory entirely.

**`correct` takes an `origin` parameter defaulting to `USER_STATED`.** This does not
violate FR-02.7's ban on promoting `AI_INFERRED` to `USER_STATED`: the original row
keeps its own origin and is retracted, while the replacement is a distinct new fact
whose source genuinely is a user statement. Exposed as a parameter so a
system-initiated correction can declare `AI_INFERRED` honestly.

## Test-count change

240 → 269. The `test_schema_consistency` helper was extended with `added_columns`,
because ADR-004 makes migrations forward-only: a column added to an existing table can
only ever appear in an `ALTER TABLE`, and checking the `CREATE TABLE` body alone
reported both new columns as missing — pushing toward rewriting an applied migration,
the one thing forward-only exists to prevent.

## Not done in this unit

- **Stale provisional-entity review.** `list_provisional()` exists and is reachable
  through `EntityService`, but nothing user-facing reads it. Carried to Unit 6
  (Management & Inspection). Unchanged from the Unit 2 note.
- **Belief history for events and relationships.** The schema's `memory_kind` supports
  them; only `fact` is written. Facts are where corrections actually arrive.
- **Live verification.** Requires the Docker machine. Migration 0003 applies
  automatically on startup.

## Activation steps

1. Sync the repository to the Docker machine.
2. Restart the application. Startup applies migration 0003 and should log
   `migrations_up_to_date count=3` and `schema_drift_check_passed tables=15`.
3. Verify the new tables exist:

       docker exec pca-postgres psql -U pca -d pca -c "\dt belief_history"
       docker exec pca-postgres psql -U pca -d pca -c "\d facts" | Select-String "corrected_from|supersedes"

4. Exercise supersession end to end: state a fact, then state a change to it, and
   confirm both rows survive with the earlier one bounded:

       docker exec pca-postgres psql -U pca -d pca -c "SELECT statement, valid_from, valid_to, retracted_at FROM facts ORDER BY created_at;"

   The superseded row must show a non-null `valid_to` and a NULL `retracted_at`. A
   non-null `retracted_at` there would mean supersession is behaving as correction.

5. Confirm the audit trail is being written:

       docker exec pca-postgres psql -U pca -d pca -c "SELECT operation, reason FROM memory_operations ORDER BY performed_at DESC LIMIT 10;"
       docker exec pca-postgres psql -U pca -d pca -c "SELECT cause, statement FROM belief_history ORDER BY recorded_at;"
