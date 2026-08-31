"""Reciprocal Rank Fusion — combining strategies whose scores are incomparable.

Layer L3. Pure function, no I/O.

The problem this solves: a cosine-similarity score of 0.82 and a BM25 score of 11.4
describe the same relevance on different scales. Summing or averaging them lets
whichever strategy emits larger numbers dominate the ranking, and normalising per
strategy only hides the issue — min-max normalisation over a handful of results is
dominated by outliers, so one strong hit compresses everything else to near zero.

RRF sidesteps this by discarding the magnitudes and using only RANK:

    score(item) = sum over strategies of  1 / (k + rank_in_that_strategy)

An item found at a mediocre rank by several strategies outranks one found first by
exactly one. That is the desired behaviour for this product: agreement across
independent retrieval methods is stronger evidence than a single confident score,
and it makes the fused ranking robust to any one strategy misbehaving.

`k` damps the advantage of the top position. At k=60 (the value from the original
RRF paper, and the de facto default in search systems) the gap between rank 1 and
rank 2 is small enough that a single strategy cannot unilaterally decide the winner,
which is the entire point of fusing.

Deliberately NOT weighted per strategy. Weights would need tuning data this project
does not have yet, and untuned weights are just RRF with an arbitrary bias. ADR-016's
diagnostics seam exists so weights can be introduced later against recorded evidence
rather than guessed at now.
"""

from __future__ import annotations

from collections.abc import Sequence

from pca.ports.graph import GraphHit

RRF_K = 60


def fuse(
    ranked_lists: Sequence[tuple[str, Sequence[GraphHit]]],
) -> list[GraphHit]:
    """Fuse per-strategy ranked lists into one ranking.

    Each input is `(strategy_name, hits_in_that_strategy's_rank_order)`. Input order
    within each list IS the rank — callers must not pre-sort by score, because that
    would rerank one strategy's output by a scale RRF is designed to ignore.

    Deduplicates by `ref`. The surviving copy keeps the highest original score seen
    for that ref, purely so downstream logging shows something meaningful; the fused
    ORDER is decided entirely by rank, never by that score.
    """
    scores: dict[str, float] = {}
    best: dict[str, GraphHit] = {}
    found_by: dict[str, set[str]] = {}

    for strategy, hits in ranked_lists:
        for rank, hit in enumerate(hits):
            if not hit.ref:
                # A hit with no ref cannot be deduplicated, so it would be counted
                # once per strategy that found it and be pushed to the top by its own
                # duplicates. Skipped rather than synthesising an id.
                continue
            scores[hit.ref] = scores.get(hit.ref, 0.0) + 1.0 / (RRF_K + rank + 1)
            found_by.setdefault(hit.ref, set()).add(strategy)
            existing = best.get(hit.ref)
            if existing is None or hit.score > existing.score:
                best[hit.ref] = hit

    ordered = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    return [best[ref] for ref, _ in ordered]


def agreement(
    ranked_lists: Sequence[tuple[str, Sequence[GraphHit]]],
) -> dict[str, list[str]]:
    """Which strategies found each ref.

    Feeds diagnostics: knowing that a hit was found by four strategies rather than
    one is the clearest available signal that retrieval is working, and it is the
    detail the completion criterion asks to surface.
    """
    found_by: dict[str, list[str]] = {}
    for strategy, hits in ranked_lists:
        for hit in hits:
            if not hit.ref:
                continue
            if strategy not in found_by.setdefault(hit.ref, []):
                found_by[hit.ref].append(strategy)
    return found_by
