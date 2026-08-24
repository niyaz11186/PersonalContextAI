"""Tests for SalienceScorer (ADR-017).

Salience exists to stop aggressive extraction (FR-02.2) from burying signal in
trivia. At 50+ messages a day the graph fills with "had coffee"; the failure mode is
not forgetting but *burying*, and retrieval precision is what the core hypothesis
depends on.

The scorer is deterministic on purpose. Asking a model for "a score between 0 and 1"
gives values that drift between identical calls and cannot be tuned or explained.
The model classifies; this computes.

These tests pin the *ordering*, which is the deliberate part, rather than the exact
weights, which are an untuned first pass.
"""

from __future__ import annotations

import pytest

from pca.domain.enums import Confidence, Origin, SalienceCategory
from pca.services.salience import MINIMUM_SALIENCE, SalienceScorer


@pytest.fixture
def scorer() -> SalienceScorer:
    return SalienceScorer()


def test_significant_life_events_outrank_passing_detail(scorer: SalienceScorer) -> None:
    """The concrete case from the design discussion: a sibling's divorce must not
    rank alongside having had toast."""
    divorce = scorer.score(SalienceCategory.SIGNIFICANT_EVENT, Origin.USER_STATED)
    toast = scorer.score(SalienceCategory.TRANSIENT, Origin.USER_STATED)

    assert divorce > toast
    assert divorce - toast > 0.5, "the gap should be decisive, not marginal"


def test_category_ordering_is_stable(scorer: SalienceScorer) -> None:
    """Durable information about people, decisions, and change outranks location and
    preference, which outrank passing detail."""
    ordered = [
        SalienceCategory.SIGNIFICANT_EVENT,
        SalienceCategory.RELATIONSHIP,
        SalienceCategory.DECISION,
        SalienceCategory.STATE_CHANGE,
        SalienceCategory.IDENTITY,
        SalienceCategory.LOCATION,
        SalienceCategory.PREFERENCE,
        SalienceCategory.TRANSIENT,
    ]
    weights = [scorer.weight_of(c) for c in ordered]

    assert weights == sorted(weights, reverse=True)


def test_user_stated_outranks_inferred(scorer: SalienceScorer) -> None:
    """An inference is worth less than a first-hand statement, but not nothing."""
    stated = scorer.score(SalienceCategory.IDENTITY, Origin.USER_STATED)
    inferred = scorer.score(SalienceCategory.IDENTITY, Origin.AI_INFERRED)

    assert stated > inferred
    assert inferred > MINIMUM_SALIENCE


def test_uncertainty_reduces_salience(scorer: SalienceScorer) -> None:
    certain = scorer.score(
        SalienceCategory.IDENTITY, Origin.USER_STATED, Confidence.CERTAIN
    )
    uncertain = scorer.score(
        SalienceCategory.IDENTITY, Origin.USER_STATED, Confidence.UNCERTAIN
    )

    assert certain > uncertain


def test_naming_entities_raises_salience(scorer: SalienceScorer) -> None:
    """Facts about identifiable people are far more often the subject of later
    questions, and far more retrievable."""
    with_entity = scorer.score(
        SalienceCategory.LOCATION, Origin.USER_STATED, involves_entities=True
    )
    without = scorer.score(
        SalienceCategory.LOCATION, Origin.USER_STATED, involves_entities=False
    )

    assert with_entity > without


def test_temporal_anchoring_raises_salience(scorer: SalienceScorer) -> None:
    """An anchored fact can join a timeline; an undated one cannot."""
    anchored = scorer.score(
        SalienceCategory.STATE_CHANGE, Origin.USER_STATED, is_temporally_anchored=True
    )
    floating = scorer.score(
        SalienceCategory.STATE_CHANGE, Origin.USER_STATED, is_temporally_anchored=False
    )

    assert anchored > floating


def test_bonuses_cannot_let_trivia_outrank_a_bereavement(scorer: SalienceScorer) -> None:
    """Bonuses are additive nudges, not multipliers.

    A perfectly-anchored, entity-naming triviality must still rank below a
    significant life event, or the whole ordering is decorative.
    """
    best_case_trivia = scorer.score(
        SalienceCategory.TRANSIENT,
        Origin.USER_STATED,
        Confidence.CERTAIN,
        involves_entities=True,
        is_temporally_anchored=True,
    )
    plain_significant_event = scorer.score(
        SalienceCategory.SIGNIFICANT_EVENT, Origin.AI_INFERRED, Confidence.UNCERTAIN
    )

    assert plain_significant_event > best_case_trivia


@pytest.mark.parametrize("category", list(SalienceCategory))
def test_score_is_always_within_bounds(
    scorer: SalienceScorer, category: SalienceCategory
) -> None:
    for origin in Origin:
        for confidence in Confidence:
            score = scorer.score(
                category,
                origin,
                confidence,
                involves_entities=True,
                is_temporally_anchored=True,
            )
            assert MINIMUM_SALIENCE <= score <= 1.0


def test_nothing_ever_scores_zero(scorer: SalienceScorer) -> None:
    """ADR-017: salience weights, it never filters.

    A score of exactly zero would let a future ranking change silently drop a memory
    entirely, which is the one thing this must not enable.
    """
    lowest = scorer.score(
        SalienceCategory.TRANSIENT, Origin.AI_INFERRED, Confidence.UNCERTAIN
    )

    assert lowest >= MINIMUM_SALIENCE
    assert lowest > 0
