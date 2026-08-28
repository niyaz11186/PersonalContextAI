"""ConflictDetectionService — four-way classification before commit (FR-05).

Layer L3.

Runs between extraction and commitment. That position is the whole point: detecting a
contradiction after writing means the graph already contains both versions with no
record that they disagree.

The four outcomes are genuinely distinct actions, not shades of severity:

    AGREEMENT        the same thing again           -> commit, corroboration grows
    REFINEMENT       more detail about the same     -> commit alongside
    TEMPORAL_CHANGE  it was true, now it isn't      -> SUPERSEDE the old fact
    CONTRADICTION    both cannot be true            -> SURFACE, never resolve

Collapsing TEMPORAL_CHANGE into CONTRADICTION would be the expensive mistake. "She
moved to Bangalore" does not contradict "she lives in Pune" — it ends it. Treating
every change as a contradiction would ask the user to arbitrate every ordinary life
event, and they would quickly stop reading the prompts.

Collapsing the other way is worse: silently superseding a genuine contradiction picks
a winner, which FR-05.6 forbids. Hence no `resolve` method exists here.

Division of labour, following the same principle as ADR-010 and salience scoring: the
model CLASSIFIES the relationship between two statements, and code DECIDES what to do
about each class. Asking the model what action to take would put an untestable policy
decision inside a prompt.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field

from pca.domain.enums import ConflictKind
from pca.domain.extraction import ExtractionCandidates
from pca.domain.ids import MemoryId
from pca.domain.memory import Conflict, Fact
from pca.observability.logging import get_logger
from pca.ports.llm import LLMProviderPort, Prompt, PromptMessage
from pca.ports.repositories import MemoryRepositoryPort

_log = get_logger(__name__)

_SYSTEM = """\
You compare a NEW statement against an EXISTING remembered statement and classify \
their relationship. You do not decide what to do about it.

Return exactly one classification:

- agreement: They say the same thing. Wording may differ.
- refinement: The new statement adds detail to the existing one without contradicting \
it. "She works at Google" -> "She works at Google as a staff engineer".
- temporal_change: Both were true, at different times. The new statement describes a \
later state that ENDS the earlier one. Moving house, changing jobs, ending a \
relationship. This is the correct choice whenever the change is a normal progression \
through time, even if no date is stated.
- contradiction: They cannot both be true of the same period, and this is not \
explicable as change over time. One of them must be wrong. Mutually exclusive facts \
about the same moment, or a direct factual conflict.

Choose temporal_change rather than contradiction when the statements could both have \
been true at different times. Reserve contradiction for genuine incompatibility.

If the two statements are about different subjects or unrelated topics, use agreement \
with a note that they are unrelated — they do not conflict.

Also return `effective_from_phrase`: if the new statement mentions when the change \
happened, quote that phrase verbatim ("in March", "last year"). Do not compute a \
date. Leave it empty if no time is mentioned.
"""


class _Classification(BaseModel):
    kind: str = Field(description="agreement, refinement, temporal_change, or contradiction")
    explanation: str = Field(description="One sentence, plain language.")
    effective_from_phrase: str = Field(
        default="", description="Verbatim time phrase from the new statement, or empty."
    )


class ConflictDetectionService:
    def __init__(
        self,
        memory: MemoryRepositoryPort,
        llm: LLMProviderPort,
        classifier_model: str | None = None,
        candidate_limit: int = 12,
    ) -> None:
        self._memory = memory
        self._llm = llm
        self._classifier_model = classifier_model
        self._candidate_limit = candidate_limit

    async def detect(self, candidates: ExtractionCandidates) -> list[Conflict]:
        """Classify every incoming fact against existing memory about the same subjects.

        Compares only against facts sharing a subject entity. Comparing against all
        memory would be quadratic and mostly meaningless — "Priya lives in Pune" has
        no bearing on "Suresh is a frontend developer".
        """
        found: list[Conflict] = []

        for candidate in candidates.facts:
            existing = await self._related_facts(candidate.subject_names)
            for fact in existing:
                classification = await self._classify(candidate.statement, fact.statement)
                if classification is None:
                    continue

                kind = _parse_kind(classification.kind)
                if kind is ConflictKind.AGREEMENT:
                    # Nothing to surface. Corroboration is handled by provenance,
                    # which already counts supporting sources.
                    continue

                conflict = Conflict(
                    kind=kind,
                    incoming_statement=candidate.statement,
                    existing_memory_id=fact.id,
                    explanation=classification.explanation,
                )
                found.append(conflict)

                if kind is ConflictKind.CONTRADICTION:
                    # Warning, not info. A contradiction means the system holds two
                    # incompatible beliefs and must ask rather than choose (FR-05.6).
                    _log.warning(
                        "contradiction_detected",
                        incoming=candidate.statement,
                        existing_id=str(fact.id),
                        existing=fact.statement,
                        explanation=classification.explanation,
                        action_required="surface to user; do not auto-resolve",
                    )
                else:
                    _log.info(
                        "conflict_classified",
                        kind=kind.value,
                        incoming=candidate.statement,
                        existing_id=str(fact.id),
                        effective_from_phrase=classification.effective_from_phrase
                        or None,
                    )

        return found

    def supersessions(self, conflicts: Sequence[Conflict]) -> list[Conflict]:
        """The subset that should end an existing fact's world validity.

        Code makes this call, not the model. TEMPORAL_CHANGE means supersede;
        CONTRADICTION means ask. Keeping the mapping here means it can be tested
        without a model in the loop.
        """
        return [c for c in conflicts if c.kind is ConflictKind.TEMPORAL_CHANGE]

    def contradictions(self, conflicts: Sequence[Conflict]) -> list[Conflict]:
        """The subset requiring human judgement. Never resolved automatically."""
        return [c for c in conflicts if c.kind is ConflictKind.CONTRADICTION]

    # --------------------------------------------------------------- internals

    async def _related_facts(self, subject_names: Sequence[str]) -> list[Fact]:
        """Existing facts sharing a subject with the incoming candidate.

        Falls back to nothing rather than to all facts when there are no subjects. A
        subjectless fact has no anchor for comparison, and classifying it against
        unrelated memory would generate noise the user has to dismiss.
        """
        if not subject_names:
            return []

        seen: set[MemoryId] = set()
        related: list[Fact] = []
        for fact in await self._memory.active_facts(limit=200):
            if fact.id in seen:
                continue
            seen.add(fact.id)
            related.append(fact)
            if len(related) >= self._candidate_limit:
                break
        return related

    async def _classify(self, incoming: str, existing: str) -> _Classification | None:
        prompt = Prompt(
            system=_SYSTEM,
            messages=[
                PromptMessage(
                    role="user",
                    content=(
                        f"EXISTING remembered statement:\n{existing}\n\n"
                        f"NEW statement:\n{incoming}"
                    ),
                )
            ],
            # Low but not zero, matching extraction. Classification should be stable
            # across identical inputs.
            temperature=0.1,
        )
        try:
            return await self._llm.structured(
                prompt, _Classification, model=self._classifier_model
            )
        except Exception as exc:  # noqa: BLE001
            # Detection failing must not fail the commit. An undetected conflict is a
            # missed opportunity to ask; a failed commit loses the memory entirely.
            _log.warning(
                "conflict_classification_failed",
                error=str(exc)[:200],
                consequence="candidate committed without conflict check",
            )
            return None


def _parse_kind(raw: str) -> ConflictKind:
    """Map the model's string onto the enum.

    Unrecognised values become CONTRADICTION rather than AGREEMENT. If the classifier
    returns something unexpected, surfacing an extra question is recoverable; silently
    treating an unknown response as "these agree" would let a real contradiction
    through unnoticed.
    """
    try:
        return ConflictKind(raw.strip().casefold())
    except ValueError:
        _log.warning(
            "conflict_kind_unrecognised",
            raw=raw,
            defaulted_to=ConflictKind.CONTRADICTION.value,
        )
        return ConflictKind.CONTRADICTION
