"""ContextAssemblyService — the explicit context package (FR-07).

Layer L3.

`assemble` builds the package; `render` turns it into prompt text. They are separate
so prompt wording can be changed and evaluated without touching retrieval, which
matters because phrasing is the thing most likely to need iteration.

The four-way distinction (FR-07.2) is enforced structurally by `ContextPackage`'s
separate fields, and `render` keeps them under separate headings. This is the main
defence against hallucinated history: the model is never shown a flat list where
"what you told me" and "what I inferred" are indistinguishable (FR-07.4).

Routing is by PRIORITY, so the buckets are disjoint:

    1. confidence is UNCERTAIN     -> uncertain          never assert this
    2. replaced an earlier record  -> currently_believed  true NOW, was different
    3. origin is USER_STATED       -> user_stated         the user's own words
    4. otherwise                   -> system_derived      our inference or an import

Priority order rather than independent predicates, because a fact can satisfy several
and appearing twice would let the model double-count corroboration it does not have.

Rule 1 outranks everything: an uncertain fact must not be presented as user-stated
just because the user said it hesitantly. Rule 2 outranks origin because "this
superseded something" is the more consequential thing to disclose — a bare "Priya
lives in Bangalore" invites a different answer than one flagged as having replaced an
earlier record.
"""

from __future__ import annotations

from collections.abc import Sequence

from pca.domain.conversation import Message, SourceExcerpt
from pca.domain.enums import Confidence, MemoryKind, Origin
from pca.domain.memory import Conflict, Fact
from pca.domain.retrieval import (
    ContextPackage,
    RetrievalResult,
    Timeline,
    TimelineEntry,
)
from pca.observability.logging import get_logger
from pca.services.provenance import ProvenanceService

_log = get_logger(__name__)

_NO_HISTORY = (
    "You have no stored history relevant to this message. Say so plainly if the "
    "user's question depends on past context. Do not invent recollections."
)

# How many facts get a source excerpt. Excerpts are the most token-expensive part of
# the package — each carries surrounding messages — so they go to the highest-salience
# facts rather than to everything.
_MAX_EXCERPTS = 3


class ContextAssemblyService:
    """Assembles and renders the context package."""

    def __init__(self, provenance: ProvenanceService | None = None) -> None:
        # Optional so the service stays constructible without a database, which the
        # render-only tests rely on. Source excerpts are simply omitted without it.
        self._provenance = provenance

    async def assemble(
        self,
        result: RetrievalResult,
        history: Sequence[Message],
        conflicts: Sequence[Conflict] = (),
    ) -> ContextPackage:
        notices: list[str] = []
        if result.diagnostics.degraded:
            # NFR-06.5 requires disclosure, not silent degradation. Carrying the
            # notice in the package means render() cannot omit it by accident.
            notices.append(
                "Stored memory could not be searched for this reply, so older "
                "context may be missing."
            )
        if result.diagnostics.stopped_early:
            notices.append(
                "The search for related history was cut short to keep the reply "
                "timely, so something relevant may have been missed."
            )

        buckets = self._split(result.facts)
        excerpts = await self._excerpts(result.facts)

        package = ContextPackage(
            user_stated=buckets[Origin.USER_STATED],
            system_derived=buckets[Origin.AI_INFERRED],
            currently_believed=buckets["currently_believed"],
            uncertain=buckets["uncertain"],
            events=list(result.events),
            entities=list(result.entities),
            relationships=list(result.relationships),
            conflicts=list(conflicts),
            source_excerpts=excerpts,
            conversation_history=list(history),
            timeline=self._timeline(result.facts),
            degradation_notices=notices,
        )

        _log.info(
            "context_assembled",
            history_messages=len(history),
            user_stated=len(package.user_stated),
            system_derived=len(package.system_derived),
            currently_believed=len(package.currently_believed),
            uncertain=len(package.uncertain),
            conflicts=len(package.conflicts),
            excerpts=len(package.source_excerpts),
            degraded=result.diagnostics.degraded,
        )
        return package

    # --------------------------------------------------------------- internals

    @staticmethod
    def _split(facts: Sequence[Fact]) -> dict:  # type: ignore[type-arg]
        """Route each fact to exactly one bucket. See the module docstring."""
        buckets: dict = {  # type: ignore[type-arg]
            Origin.USER_STATED: [],
            Origin.AI_INFERRED: [],
            "currently_believed": [],
            "uncertain": [],
        }

        for fact in facts:
            if fact.confidence is Confidence.UNCERTAIN:
                buckets["uncertain"].append(fact)
            elif fact.has_history:
                buckets["currently_believed"].append(fact)
            elif fact.origin is Origin.USER_STATED:
                buckets[Origin.USER_STATED].append(fact)
            else:
                # AI_INFERRED and IMPORTED both land here. Neither is the user's own
                # assertion, which is the distinction FR-07.2 draws and FR-02.7
                # forbids blurring.
                buckets[Origin.AI_INFERRED].append(fact)

        return buckets

    async def _excerpts(self, facts: Sequence[Fact]) -> list[SourceExcerpt]:
        """Source excerpts for the most salient facts (FR-07.4, FR-09.3).

        Excerpts are what let a user check a remembered fact against what they
        actually said. Capped because each one carries surrounding messages and would
        otherwise dominate the context budget.
        """
        if self._provenance is None:
            return []

        ranked = sorted(facts, key=lambda f: f.salience, reverse=True)[:_MAX_EXCERPTS]
        excerpts: list[SourceExcerpt] = []
        for fact in ranked:
            if not fact.provenance:
                continue
            try:
                excerpt = await self._provenance.source_excerpt(fact.provenance[0])
            except Exception as exc:  # noqa: BLE001
                # A missing excerpt weakens verifiability but must not fail the
                # reply; the fact itself is still authoritative.
                _log.warning(
                    "source_excerpt_unavailable",
                    fact_id=str(fact.id),
                    error=str(exc)[:200],
                )
                continue
            if excerpt.messages:
                excerpts.append(excerpt)
        return excerpts

    @staticmethod
    def _timeline(facts: Sequence[Fact]) -> Timeline | None:
        """Chronology of the dated facts (FR-07.3).

        Only facts with a resolved world-time date appear. ADR-010 leaves dates null
        rather than guessing, so an undated fact has no position on a timeline and
        placing it anywhere would be an invention.
        """
        entries = [
            TimelineEntry(
                when=fact.validity.valid_from,
                description=fact.statement,
                is_uncertain=fact.confidence is Confidence.UNCERTAIN,
            )
            for fact in facts
            if fact.validity.valid_from is not None
        ]
        if not entries:
            return None
        entries.sort(key=lambda e: e.when)  # type: ignore[arg-type,return-value]
        return Timeline(entries=entries)

    # ----------------------------------------------------------------- render

    def render(self, package: ContextPackage) -> str:
        """Render the package as prompt text.

        Every section is explicitly labelled by epistemic status. An unlabelled block
        would let the model treat its own earlier inference as something the user
        asserted, which is exactly the failure FR-07.4 targets.
        """
        sections: list[str] = []

        if package.degradation_notices:
            sections.append(
                "## Reliability notice\n"
                + "\n".join(f"- {n}" for n in package.degradation_notices)
                + "\nTell the user about this limitation if it affects your answer."
            )

        if package.user_stated:
            sections.append(
                "## Stated by the user (treat as fact)\n"
                + "\n".join(f"- {f.statement}" for f in package.user_stated)
            )

        if package.currently_believed:
            sections.append(
                "## Current state (these replaced earlier records)\n"
                "Each of these is what is true now; something different was true "
                "before. If the user asks about the past, say the record changed "
                "rather than implying it was always this way.\n"
                + "\n".join(f"- {f.statement}" for f in package.currently_believed)
            )

        if package.system_derived:
            sections.append(
                "## Derived by the system (not stated by the user)\n"
                "Attribute these as your own inference if you use them.\n"
                + "\n".join(f"- {f.statement}" for f in package.system_derived)
            )

        if package.uncertain:
            sections.append(
                "## Uncertain — do not assert these\n"
                "Ask the user rather than stating any of these as fact.\n"
                + "\n".join(f"- {f.statement}" for f in package.uncertain)
            )

        if package.relationships:
            sections.append(
                "## Known relationships\n"
                + "\n".join(
                    f"- {r.relation_type}" for r in package.relationships
                )
            )

        if package.timeline and package.timeline.entries:
            sections.append(
                "## Chronology (dated records only)\n"
                + "\n".join(
                    f"- {e.when:%Y-%m-%d}: {e.description}"
                    + (" [uncertain]" if e.is_uncertain else "")
                    for e in package.timeline.entries
                    if e.when
                )
            )

        if package.conflicts:
            # FR-05.6: surface the conflict, never pick a winner.
            sections.append(
                "## Conflicting records — surface the conflict, do not choose\n"
                + "\n".join(
                    f"- {c.kind.value}: {c.incoming_statement} ({c.explanation})"
                    for c in package.conflicts
                )
            )

        if package.source_excerpts:
            sections.append(
                "## Source excerpts (what the user actually said)\n"
                + "\n\n".join(
                    "\n".join(f"  {m.role.value}: {m.content}" for m in ex.messages)
                    for ex in package.source_excerpts
                )
            )

        if package.conversation_history:
            sections.append(
                "## Current conversation\n"
                + "\n".join(
                    f"{m.role.value}: {m.content}" for m in package.conversation_history
                )
            )

        if not sections:
            return _NO_HISTORY

        return "\n\n".join(sections)
