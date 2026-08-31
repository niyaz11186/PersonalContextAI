# Unit 4 — Retrieval Depth — Completion Summary

**Status**: CODE COMPLETE. 310 tests passing offline. Not yet verified live.
**Date**: 2026-08-30

## Completion criterion

> A question that requires history returns a small, relevant context package.
> Diagnostics show which strategies contributed and what the governor discarded.

Both halves are asserted. The second half of the first sentence is the one that is
easy to fail while appearing to succeed: **returning everything similar also returns
the right answer**, so a test that only checks the fact is present cannot tell good
retrieval from a dump. Smallness is therefore asserted separately —
`test_the_package_is_bounded_by_max_items` commits 25 facts and requires at most 3
back, with `dropped_by_budget > 0`.

Diagnostics: `test_diagnostics_report_which_strategies_contributed` asserts
per-strategy attribution names strategies individually, on the second message rather
than the first (see "Discovered during implementation" below).

## The bug this unit was actually hiding

`RetrievalResult.facts` **was always empty** — through Units 1b, 2, and 3. Retrieval
returned only diagnostics, and the context the model saw was raw Graphiti edge text
passed through a `raw_hits` side channel labelled "unverified".

So the assistant's answers were built from Graphiti's paraphrase of what the user
said, not from the committed facts in PostgreSQL. That inverts ADR-015: the graph
became the effective source of truth for anything the user was told, while PostgreSQL
held records nobody read. It was invisible because the answers were still roughly
right — Graphiti's paraphrase usually resembles the original.

`retrieve()` now resolves graph candidates against PostgreSQL and returns typed
`Fact` objects. `raw_hits` is deleted.

## What was built

| Area | Deliverable |
|---|---|
| Strategies | Five, each an explicit Graphiti `SearchConfig`: `cosine_similarity` (semantic), `bm25` (full-text), `node_distance` centred (entity), `valid_at`/`invalid_at` filtered (temporal), `bfs` (traversal) |
| Fusion | `services/fusion.py` — Reciprocal Rank Fusion, k=60 |
| Budget | `services/budget.py` — `RetrievalBudgetGovernor`, pure policy |
| Rerank | `GraphitiMemoryAdapter.rerank` via the injected `GeminiRerankerClient`, capped |
| Assembly | Four disjoint buckets, source excerpts, chronology, conflicts |
| Diagnostics | Per-strategy `StrategySpend` with `failed`, plus `stopped_early` / `stop_reason` |

## Design decisions worth recording

**RRF, not score fusion.** A cosine score of 0.82 and a BM25 score of 11.4 describe
the same relevance on different scales; summing them lets whichever strategy emits
larger numbers decide the ranking. Per-strategy min-max normalisation only hides it —
over a handful of results one outlier compresses everything else to near zero. RRF
discards magnitudes and uses rank alone, so agreement across independent strategies
outranks a single confident score. That is the right bias for this product:
`test_rrf_prefers_agreement_over_a_single_confident_hit` pins it.

**Unweighted.** Per-strategy weights need tuning data this project does not have, and
untuned weights are RRF with an arbitrary bias. ADR-016's diagnostics seam exists so
weights can later be fitted against recorded evidence.

**Unit 1b's `search_semantic` was not semantic.** It called Graphiti's default
`search()`, which is internally hybrid (cosine + BM25 + BFS). Attributing those
results to "semantic" made per-strategy diagnostics fiction — full-text and traversal
were already folded in and could not be measured or disabled. Each strategy now
declares exactly one search method.

**Each strategy reranks with `rrf`, not `cross_encoder`.** We fuse across strategies
ourselves and cross-encode once at the end. Letting Graphiti cross-encode per strategy
would multiply reranker cost by five to produce rankings we immediately discard.

**Reranking is capped at 20 candidates.** `GeminiRerankerClient.rank` issues **one API
call per passage** (confirmed by reading the installed source). Uncapped, reranking a
60-hit fused set is 60 Gemini calls and becomes the dominant latency cost rather than
a refinement. Candidates beyond the cutoff keep their fused rank and are appended —
nothing is silently dropped by the reranker; trimming is the governor's job.

**`should_continue` is not gated by `max_items`.** Gathering more candidates than we
keep is the point: fusion and reranking need a surplus to choose from, and stopping at
exactly `max_items` hands the reranker a set it cannot improve.

**Duration headroom of 0.75.** A stage authorised at 99% of budget still overruns it;
the check has to leave room for the work it is approving.

**Trim keeps one oversized item rather than returning nothing.** An empty context is
indistinguishable from a retrieval failure.

**Four buckets routed by priority, not independent predicates.** A fact can satisfy
several, and appearing twice would let the model double-count corroboration it does
not have. Order: uncertain → replaced-an-earlier-record → user-stated → derived.
Uncertainty outranks origin so a hesitantly-stated fact is not presented as
authoritative; history outranks origin because "this superseded something" changes
what a question about the past should be answered with.

**`Fact.supersedes` and `Fact.corrected_from` added to the domain.** Migration 0003
wrote these columns; nothing read them. Without them `currently_believed` has no
meaning distinct from `user_stated`, because a fact that never superseded anything is
the current belief by default.

**`degraded` is ANY strategy failing, not all.** Caught by a regression: with the graph
down but one strategy returning an empty list without raising, `all(failed)` was False
and the system reported healthy retrieval over missing memory. Each strategy exists to
catch what the others miss, so losing one genuinely means context may be absent
(NFR-06.5). Erring toward disclosure is correct for a system whose core risk is
answering confidently from incomplete memory.

## Bugs found in existing code

**Entity-scoped search never worked.** `search_by_entity` passed `str(entity_id)` as
Graphiti's `center_node_uuid`. Our `EntityId` and Graphiti's node uuid come from two
independent extraction passes over the same text and never coincide, so the centre
node matched nothing and the strategy returned **unscoped** results — appearing to
work while contributing nothing scoped. The port now takes a name; `RetrievalService`
resolves id → name via `EntityRepositoryPort`. Recorded as C-30.

**Temporal filtering had to express overlap, not containment.** A fact valid from
January with no end date is still true in March. Filtering on `valid_at` falling
inside the window would miss exactly the long-running facts most worth retrieving —
where someone lives, who they work for. The fake models overlap too, so a containment
bug cannot pass.

## Discovered during implementation

**Retrieval runs before commit on the request path.** The first message in a
conversation always retrieves against an empty store: every strategy runs, finds
nothing, contributes nothing. That is a legitimate third state alongside "contributed"
and "failed", and an initial integration assertion demanding contribution on a cold
start was wrong — the design does not promise it. The test now asserts cold and warm
behaviour separately.

**Graph-to-Postgres matching is by normalised statement text, and it is imperfect.**
Graphiti rewrites extracted facts in its own words, so its edge text and our
`facts.statement` are independent paraphrases of the same utterance. A shared id would
be better but none exists — Graphiti assigns edge ids during its own pass with no
knowledge of our commit. Rather than pretend the match is reliable, unmatched hits
contribute ranking signal only and the ratio is recorded in
`diagnostics.notes`; a ratio near 100% means the seam has broken and ranking is doing
nothing. Facts the graph never indexed are still returned (salience-ordered), so
retrieval quality is not hostage to Graphiti's extraction.

## Not done in this unit

- **`retrieval_diagnostics` persistence.** The `PCA_PERSIST_RETRIEVAL_DIAGNOSTICS`
  setting exists and `RetrievalDiagnostics` remains a clean serialisable dataclass,
  but the table and repository are Unit 7 per `unit-of-work-dependency.md`. Not
  over-built here.
- **`budget_for(intent)` varying by intent.** `Intent` is Unit 5's `IntentRouter`. The
  signature accepts `str | None` and ignores it, so Unit 5 can add routing without
  changing call sites — rather than inventing a placeholder type this unit cannot use.
- **Events in retrieval.** `RetrievalResult.events` stays empty; extraction produces
  few events so far and facts are where the value is. Flagged rather than hidden.
- **Live verification.** Requires the Docker machine.

## Activation steps

1. Sync and restart. No migration in this unit, so no schema change.
2. Send a message stating a fact, then in a NEW conversation ask about it.
3. Confirm the log line now reports per-strategy attribution:

       {"event": "retrieval_complete", "contributing": ["semantic", "fulltext"], ...}

   `contributing` empty on a warm store with relevant facts means fusion or the
   text-matching seam is broken.

4. Confirm `facts` is non-zero in `retrieval_complete`. It was structurally always 0
   before this unit; a persistent 0 with committed facts means resolution is failing.

5. Watch for `rerank_capped` and `budget_trimmed`. Their absence on a large store
   means the governor is not engaging.

6. Check latency. Five strategies plus a capped rerank is more model calls than Unit
   1b's single fused search. If `ms` approaches the 25s budget, lower
   `MAX_RERANK_CANDIDATES` first — it is the dominant cost.
