"""Salience scoring (ADR-017).

Pure and deterministic. No model calls.

The division of labour mirrors ADR-010's handling of time: the model **classifies**
(this is a relationship / a decision / passing chatter) and our code **computes** the
number. Asking a model directly for "a salience score between 0 and 1" produces
values that drift between calls on identical input, cannot be tuned coherently, and
cannot be explained when a retrieval result looks wrong.

Why salience exists at all: FR-02.2 mandates aggressive extraction, and at 50+
messages a day that fills the graph with "had coffee" and "was tired". The failure
mode is not forgetting — it is **burying**. Retrieval precision collapses when
signal is diluted by trivia, and precision is what the core hypothesis depends on.

Salience **weights ranking; it never filters.** Nothing is discarded for being
low-salience, because today's trivia is occasionally next year's important detail.
"""

from __future__ import annotations

from pca.domain.enums import Confidence, Origin, SalienceCategory

# Starting weights. These are a considered first pass, not a tuned result — real
# tuning needs a corpus of the user's own history, which does not exist yet. The
# ordering is the deliberate part: durable facts about people, decisions, and
# changes outrank location and preference, which outrank passing detail.
_CATEGORY_WEIGHT: dict[SalienceCategory, float] = {
    SalienceCategory.SIGNIFICANT_EVENT: 0.95,
    SalienceCategory.RELATIONSHIP: 0.90,
    SalienceCategory.DECISION: 0.85,
    SalienceCategory.COMMITMENT: 0.85,
    SalienceCategory.STATE_CHANGE: 0.80,
    SalienceCategory.IDENTITY: 0.75,
    SalienceCategory.LOCATION: 0.60,
    SalienceCategory.PREFERENCE: 0.50,
    SalienceCategory.TRANSIENT: 0.15,
}

# An inference is worth less than a first-hand statement, but not worthless.
_ORIGIN_FACTOR: dict[Origin, float] = {
    Origin.USER_STATED: 1.0,
    Origin.IMPORTED: 0.95,
    Origin.AI_INFERRED: 0.75,
}

_CONFIDENCE_FACTOR: dict[Confidence, float] = {
    Confidence.CERTAIN: 1.0,
    Confidence.PROBABLE: 0.9,
    Confidence.UNCERTAIN: 0.7,
}

# Floor rather than zero. A score of exactly 0 would let a future ranking change
# silently drop a memory entirely, and ADR-017 is explicit that salience weights
# rather than filters.
MINIMUM_SALIENCE = 0.05


class SalienceScorer:
    """Turns a classification into a score. Stateless."""

    def score(
        self,
        category: SalienceCategory,
        origin: Origin,
        confidence: Confidence = Confidence.PROBABLE,
        involves_entities: bool = False,
        is_temporally_anchored: bool = False,
    ) -> float:
        """Compute salience in 0.0..1.0.

        Args:
            category: what kind of information this is.
            origin: user-stated outranks inferred.
            confidence: how sure the extraction was.
            involves_entities: whether it names people, places, or organizations.
                Facts about identifiable entities are far more retrievable and far
                more often the subject of later questions.
            is_temporally_anchored: whether it resolved to a real date. An anchored
                fact can participate in timeline reconstruction; an undated one
                cannot, which makes it less useful even when the content matters.
        """
        base = _CATEGORY_WEIGHT[category]
        base *= _ORIGIN_FACTOR[origin]
        base *= _CONFIDENCE_FACTOR[confidence]

        # Small additive bonuses rather than multipliers: they should nudge ordering
        # within a category, not let a well-anchored triviality outrank a bereavement.
        if involves_entities:
            base += 0.05
        if is_temporally_anchored:
            base += 0.05

        return max(MINIMUM_SALIENCE, min(1.0, round(base, 4)))

    @staticmethod
    def weight_of(category: SalienceCategory) -> float:
        """Exposed so tests and tuning can inspect the table directly."""
        return _CATEGORY_WEIGHT[category]
