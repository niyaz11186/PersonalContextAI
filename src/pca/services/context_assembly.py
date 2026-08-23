"""ContextAssemblyService — naive version for Unit 1b.

Layer L3.

`assemble` builds the package; `render` turns it into prompt text. They are
separate so prompt wording can be changed and evaluated without touching
retrieval, which matters because prompt phrasing is the thing most likely to need
iteration.

The four-way distinction (FR-07.2) is enforced structurally by ContextPackage's
separate fields, and `render` keeps them under separate headings. This is the main
defence against hallucinated history: the model is never shown a flat list where
"what you told me" and "what I inferred" are indistinguishable.
"""

from __future__ import annotations

from collections.abc import Sequence

from pca.domain.conversation import Message
from pca.domain.retrieval import ContextPackage, RetrievalResult
from pca.observability.logging import get_logger
from pca.ports.graph import GraphHit

_log = get_logger(__name__)

_NO_HISTORY = (
    "You have no stored history relevant to this message. Say so plainly if the "
    "user's question depends on past context. Do not invent recollections."
)


class ContextAssemblyService:
    """Assembles and renders the context package."""

    async def assemble(
        self,
        result: RetrievalResult,
        history: Sequence[Message],
        raw_hits: Sequence[GraphHit] | None = None,
    ) -> ContextPackage:
        notices: list[str] = []
        if result.diagnostics.degraded:
            # NFR-06.5 requires disclosure, not silent degradation. Carrying the
            # notice in the package means render() cannot omit it by accident.
            notices.append(
                "Stored memory could not be searched for this reply, so older "
                "context may be missing."
            )

        package = ContextPackage(
            user_stated=list(result.facts),
            events=list(result.events),
            entities=list(result.entities),
            relationships=list(result.relationships),
            conversation_history=list(history),
            degradation_notices=notices,
        )
        _log.info(
            "context_assembled",
            history_messages=len(history),
            raw_hits=len(raw_hits or []),
            degraded=result.diagnostics.degraded,
        )
        return package

    def render(
        self,
        package: ContextPackage,
        raw_hits: Sequence[GraphHit] | None = None,
    ) -> str:
        """Render the package as prompt text.

        Every section is explicitly labelled by epistemic status. An unlabelled
        block would let the model treat its own earlier inference as something the
        user asserted, which is exactly the failure FR-07.4 targets.
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

        if package.system_derived:
            sections.append(
                "## Derived by the system (not stated by the user)\n"
                + "\n".join(f"- {f.statement}" for f in package.system_derived)
            )

        if package.uncertain:
            sections.append(
                "## Uncertain — do not assert these\n"
                + "\n".join(f"- {f.statement}" for f in package.uncertain)
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

        if raw_hits:
            # Naive-only section. Unit 3 replaces it with typed facts once the
            # memory write path exists; labelled as unverified until then.
            sections.append(
                "## Possibly relevant stored material (unverified, retrieved by similarity)\n"
                + "\n".join(f"- {h.content}" for h in raw_hits)
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
