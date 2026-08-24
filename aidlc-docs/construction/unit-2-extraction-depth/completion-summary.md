# Unit 2 — Extraction Depth: Completion Summary

**Status**: CODE COMPLETE (offline). Awaiting activation against live infrastructure.
**Date**: 2026-08-24
**Verification**: 198 tests passing, up from 132.

---

## What motivated this unit

Unit 1b's live test recalled two of four stated facts:

| Stated | Recalled by Unit 1b |
|---|---|
| Suresh is a **friend** | No |
| Suresh is a **frontend developer** | No |
| Lives in Visakhapatnam | Yes |
| Andhra Pradesh | Yes |

The naive extractor captured the location and dropped the relationship and the
occupation. Unit 2 addresses that directly.

---

## What was built

| Area | Deliverable |
|---|---|
| Schema | `migrations/0002_memory_model.sql` — entities, entity_aliases, facts, fact_subjects, events, event_participants, relationships, provenance_index |
| Ports | `EntityRepositoryPort`, `MemoryRepositoryPort`, `ProvenanceRepositoryPort` |
| Adapters | `adapters/postgres/memory_repositories.py` — three repositories, SQLAlchemy Core |
| Entity resolution | `services/entities.py` — the ADR-014 policy |
| Salience | `services/salience.py` — deterministic scoring from a model-supplied category |
| Provenance | `services/provenance.py` — chains, excerpts, corroboration counts |
| Write path | `services/memory.py` — `MemoryService.commit` |
| Extraction | `services/extraction.py` rewritten: entities, facts, events, relationships |
| Graphiti ontology | `adapters/graphiti/entity_types.py` — Person, Organization, Place, Project (ADR-015) |
| Wiring | composition root and the message flow now extract and commit after each reply |

---

## The two divisions of labour, held consistently

Unit 2 reuses the pattern ADR-010 established for time, because it worked:

| Concern | Model supplies | Code computes |
|---|---|---|
| Time (ADR-010) | the structure of a phrase | the dates, in `TimeResolver` |
| Salience (ADR-017) | a category | the score, in `SalienceScorer` |

Asking a model directly for "a salience score between 0 and 1" produces values that
drift between identical calls, cannot be tuned coherently, and cannot be explained
when a retrieval result looks wrong. A category plus a weight table can be all three.

---

## Entity resolution — the consequential part

ADR-014 implemented as specified. The asymmetry is the whole argument:

- a **duplicate** entity is visible, annoying, and fixable at any time
- a **wrongly merged** entity is invisible corruption of every future answer about
  either person, and near-impossible to untangle after months of accumulated facts

So: link on a single confident match, **create a provisional duplicate on ambiguity**,
never merge as a side effect of extraction. Merging is always explicit and reversible.

`test_two_matching_entities_creates_a_provisional_instead_of_guessing` is the
guard. If it ever fails because someone "improved" resolution by picking the highest
score, the system has quietly acquired its worst failure mode.

Provisional entities are listable, so the duplicates this deliberately creates cannot
accumulate unseen — otherwise a visible problem becomes an invisible one again.

---

## Applying the Unit 1b lesson

The Unit 1b defect was silent: a broken pipeline and a working one were
indistinguishable. Unit 2 was built to avoid repeating that.

| Mechanism | Purpose |
|---|---|
| `CommitReceipt` | A commit reports what it wrote. A commit that silently writes nothing is now observable. |
| User-facing notices | Ambiguous entities and failed memory writes appear in the SSE `done` event, not only in logs |
| End-to-end test | `test_sending_a_message_commits_facts_and_entities` asserts the authoritative store actually received data, not merely that the request returned 200 |

---

## Defects found and fixed during the unit

Four, all caught by writing the tests rather than by running the system:

1. **Tautological SQL constraint.** `entities_merge_is_consistent` as first written was
   `(a IS NULL AND b IS NULL) OR (a IS NULL AND b IS NULL) IS NOT TRUE` — always true.
   Replaced with `(merged_into IS NULL) = (merged_at IS NULL)`.
2. **`Relationship` had no id.** Provenance could not point at one, so relationships
   would have been the single memory kind silently exempt from ADR-012's corroboration
   rule. Added `id`, plus a constraint against self-links.
3. **`resolve_many` deduplicated case-sensitively** while `MemoryService` deduplicated
   case-insensitively. "Priya" and "priya" triggered two lookups. The two are now
   consistent.
4. **Variable shadowing in the SSE handler.** Naming the terminal event dict `payload`
   shadowed the request body captured from the enclosing scope, turning the earlier
   `payload.content` read into an `UnboundLocalError`. Renamed, with a comment
   explaining why the name is unavailable.

Also improved: `build_container` now validates secrets before constructing adapters,
so a missing `GOOGLE_API_KEY` produces `missing required configuration: GOOGLE_API_KEY`
instead of an opaque `ValueError` from inside the Google SDK.

---

## Known gaps, stated rather than glossed

| Gap | Disposition |
|---|---|
| `commit` is not transactional across all its writes | Unit 3. The transaction boundary arrives with the belief-history and operation-log writes that must be atomic with the memory rows |
| No conflict detection | Unit 3. Extraction returns candidates precisely so detection can run before the write |
| No correction, supersession, or retraction | Unit 3 |
| Extraction still runs before the terminal SSE event | The NFR-02.3 exception carried from Unit 1b. ADR-008's `ExtractionCoordinator` in Unit 5 moves it off the request |
| Salience weights are untuned | Deliberate. Real tuning needs a corpus of the user's own history, which does not exist yet. The *ordering* is the considered part |
| Provenance is a placeholder on hydrated facts | `_hydrate_fact` fills one synthetic ref rather than joining provenance on every read. `ProvenanceService` is the real path |

---

## Requirements advanced

| ID | Now addressed |
|---|---|
| FR-02.1, FR-02.2 | Full extraction across four record kinds |
| FR-02.4, FR-02.7 | Origin set once, immutable, no promote operation exists |
| FR-02.5 | Provenance recorded for every fact, event, and relationship |
| FR-03.1, FR-03.2, FR-03.3 | Entities, typed relationships, event participation |
| FR-03.4 | Explicit reversible merge with alias carry-across |
| FR-04.1, FR-04.2 | Both time axes persisted independently |
| ADR-014 | Never silently merge |
| ADR-015 | Custom Graphiti entity types |
| ADR-017 | Salience scoring |

---

## Next: activation

Migration `0002` has never been applied. On the Docker machine:

1. Restart the app — `MigrationRunner` applies `0002` automatically
2. Send the same Suresh message as before
3. Query PostgreSQL to confirm facts, entities, and relationships were written
4. Ask about Suresh in a fresh conversation and compare recall against Unit 1b

The specific improvement to look for: the **friend relationship** and the
**frontend developer** occupation should now be captured, not just the location.
