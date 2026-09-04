"""ClarificationWorkflow — asking rather than guessing (ADR-014, FR-05.6).

Layer L2. Imports `langgraph`.

This is the one place ADR-006's LangGraph dependency actually earns its place. Every
other workflow in this system is a linear pipeline that could be a function; this one
genuinely pauses mid-execution, survives a process restart, and resumes with its state
intact. That is the capability the dependency was taken on for.

**The hard rule: no memory is written before the user answers.** ADR-014 forbids
resolving ambiguity on the system's own authority, and the failure it protects against
is asymmetric in a way worth restating:

    a duplicate entity   -> visible, annoying, correctable at any time
    a wrongly merged one -> invisible corruption. Every future answer about either
                            person is contaminated, and untangling it after months of
                            accumulated facts is close to impossible.

So the graph is arranged so that the write is physically unreachable without passing
through the interrupt. `_apply` sits after `_ask` on the only path to it — there is no
edge that reaches a write node without the answer in state. That is a structural
guarantee rather than a conditional the next editor could invert by accident.

Triggered by `CommitReceipt.needs_clarification`, which Unit 2 has been setting since
extraction depth landed and which until now was turned into a passive notice string
the user could do nothing with.

An unrecognised answer ABANDONS rather than guessing. Reaching this workflow already
means the system could not decide; treating "hmm not sure" as a merge instruction
would defeat the entire point of stopping to ask.
"""

from __future__ import annotations

from typing import Annotated, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from pca.domain.enums import ClarificationStatus
from pca.domain.errors import ClarificationNotFound
from pca.domain.ids import EntityId
from pca.domain.orchestration import AmbiguityContext, ClarificationOutcome
from pca.observability.logging import get_logger
from pca.orchestration.checkpointer import PostgresCheckpointSaver
from pca.services.entities import EntityService

_log = get_logger(__name__)

# Replies that mean "none of these" rather than naming an option. Kept explicit
# because the alternative — treating anything unmatched as a decline — would silently
# discard a user who answered with a name spelled slightly differently.
_DECLINE = frozenset(
    {
        "none",
        "neither",
        "no",
        "nobody",
        "not sure",
        "unsure",
        "dunno",
        "don't know",
        "dont know",
        "skip",
        "cancel",
        "new",
        "someone else",
    }
)


def _keep_last(_existing: object, new: object) -> object:
    return new


class ClarificationState(TypedDict, total=False):
    ambiguity: AmbiguityContext
    answer: Annotated[str, _keep_last]
    chosen_entity_id: Annotated[EntityId | None, _keep_last]
    resolution: Annotated[str, _keep_last]
    merged_into: Annotated[EntityId | None, _keep_last]


class ClarificationWorkflow:
    def __init__(
        self,
        entities: EntityService,
        checkpointer: PostgresCheckpointSaver,
    ) -> None:
        self._entities = entities
        self._checkpointer = checkpointer
        self._graph = self._build()

    # ------------------------------------------------------------------- graph

    def _build(self):  # type: ignore[no-untyped-def]
        builder = StateGraph(ClarificationState)
        builder.add_node("ask", self._ask)
        builder.add_node("apply", self._apply)

        builder.add_edge(START, "ask")
        # The only edge into `apply` comes from `ask`. Structural, not conditional:
        # there is no path that reaches the write without the interrupt having
        # returned an answer, so ADR-014 cannot be violated by editing a predicate.
        builder.add_edge("ask", "apply")
        builder.add_edge("apply", END)

        # The checkpointer is what makes the pause durable. Without it the interrupt
        # would live only in this process's memory and a restart would lose both the
        # question and the provisional entity it was about.
        return builder.compile(checkpointer=self._checkpointer)

    # ------------------------------------------------------------------- nodes

    async def _ask(self, state: ClarificationState) -> ClarificationState:
        """Pause and put the question to the user.

        Execution stops inside `interrupt` and does not resume until `resume()` is
        called — possibly minutes later, possibly in a different process.
        """
        ambiguity = state["ambiguity"]
        answer = interrupt(
            {
                "question": ambiguity.question,
                "options": list(ambiguity.options),
                "conversation_id": str(ambiguity.conversation_id),
            }
        )
        return {"answer": str(answer)}

    async def _apply(self, state: ClarificationState) -> ClarificationState:
        """Act on the answer. Reached only after `_ask` returned one.

        Matching is on name, against the options the question offered. A reply that
        matches nothing is treated as a decline rather than as the first option —
        acting on an unrecognised answer would be guessing, which is what this
        workflow exists to avoid.
        """
        ambiguity = state["ambiguity"]
        answer = state.get("answer", "")
        normalised = answer.strip().casefold()

        if not normalised or normalised in _DECLINE:
            _log.info(
                "clarification_declined",
                conversation_id=str(ambiguity.conversation_id),
                answer=answer[:80],
                consequence="provisional entity retained unmerged for later review",
            )
            return {"resolution": ClarificationStatus.ABANDONED.value}

        chosen = await self._match(normalised, ambiguity)
        if chosen is None:
            _log.warning(
                "clarification_answer_unmatched",
                answer=answer[:80],
                options=list(ambiguity.options),
                consequence="treated as a decline; nothing merged",
            )
            return {"resolution": ClarificationStatus.ABANDONED.value}

        # The write, finally authorised. `EntityService.merge` records the operation
        # and keeps the absorbed row, so even a mistaken answer stays reversible.
        merged_into = await self._merge_provisional(chosen, ambiguity, answer)

        return {
            "chosen_entity_id": chosen,
            "merged_into": merged_into,
            "resolution": ClarificationStatus.RESOLVED.value,
        }

    # --------------------------------------------------------------- internals

    async def _match(
        self, normalised: str, ambiguity: AmbiguityContext
    ) -> EntityId | None:
        """Resolve the user's answer to one entity id.

        Answers may be the entity name or a 1-based index into the options, because
        both are natural replies to a numbered list and rejecting either would make
        the system feel obtuse about something it just asked.
        """
        options = list(ambiguity.options)

        if normalised.isdigit():
            index = int(normalised) - 1
            if 0 <= index < len(options):
                normalised = options[index].strip().casefold()
            else:
                return None

        for option in options:
            if option.strip().casefold() == normalised:
                found = await self._entities.find(option)
                if found:
                    return found[0].id
                return None

        # Not one of the offered options. Try a direct lookup, so a correct name the
        # question happened not to list still works.
        found = await self._entities.find(normalised)
        return found[0].id if found else None

    async def _merge_provisional(
        self, keep: EntityId, ambiguity: AmbiguityContext, answer: str
    ) -> EntityId | None:
        """Fold the provisional duplicate into the entity the user picked.

        Returns the surviving id, or None when there was no provisional entity to
        absorb — the user may simply have been confirming which of several existing
        people was meant, with nothing to clean up.
        """
        provisional = await self._entities.list_provisional()
        target = next(
            (
                entity
                for entity in provisional
                if entity.id != keep
                and entity.name.strip().casefold()
                in {option.strip().casefold() for option in ambiguity.options}
            ),
            None,
        )
        if target is None:
            return keep

        await self._entities.merge(
            keep=keep,
            absorb=target.id,
            reason=f"user clarification: {answer.strip()[:120]}",
        )
        _log.info(
            "clarification_resolved_by_merge",
            kept=str(keep),
            absorbed=str(target.id),
            conversation_id=str(ambiguity.conversation_id),
        )
        return keep

    # ------------------------------------------------------------------ public

    async def run(self, ambiguity: AmbiguityContext) -> ClarificationOutcome:
        """Put the question and stop.

        Returns AWAITING_ANSWER with a thread id. Nothing has been written at this
        point and nothing will be until `resume` is called.
        """
        thread_id = f"clarification:{uuid4()}"
        config = {"configurable": {"thread_id": thread_id}}

        final: ClarificationState = await self._graph.ainvoke(
            {"ambiguity": ambiguity}, config
        )
        return self._to_outcome(final, thread_id)

    async def resume(self, thread_id: str, answer: str) -> ClarificationOutcome:
        """Continue with the user's answer.

        Works across a process restart: the state is reconstructed from the
        checkpoint, not from memory held by whichever process asked the question.
        """
        config = {"configurable": {"thread_id": thread_id}}
        # `aget_state` NEVER returns None. For an unknown thread LangGraph returns a
        # StateSnapshot with `values={}`, `next=()`, and `created_at=None`, which is
        # truthy — so an `is None` check silently passes and the graph then starts
        # from scratch, entering `_ask` with no ambiguity in state and raising a bare
        # KeyError. Verified against langgraph 1.2 rather than assumed.
        snapshot = await self._graph.aget_state(config)
        if snapshot is None or snapshot.created_at is None:
            raise ClarificationNotFound(
                f"no clarification in progress for {thread_id}"
            )

        final: ClarificationState = await self._graph.ainvoke(
            Command(resume=answer), config
        )
        return self._to_outcome(final, thread_id)

    @staticmethod
    def _to_outcome(
        final: ClarificationState, thread_id: str
    ) -> ClarificationOutcome:
        interrupts = final.get("__interrupt__")  # type: ignore[call-overload]
        if interrupts:
            payload = interrupts[0].value
            return ClarificationOutcome(
                thread_id=thread_id,
                status=ClarificationStatus.AWAITING_ANSWER,
                question=payload.get("question"),
            )

        resolution = final.get("resolution", ClarificationStatus.ABANDONED.value)
        return ClarificationOutcome(
            thread_id=thread_id,
            status=ClarificationStatus(resolution),
            answer=final.get("answer"),
            applied_memory_id=final["ambiguity"].memory_id
            if final.get("ambiguity")
            else None,
        )
