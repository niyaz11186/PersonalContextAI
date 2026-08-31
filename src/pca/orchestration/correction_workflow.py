"""CorrectionWorkflow — applying a correction the user asked for.

Layer L2. Imports `langgraph`.

The whole workflow exists to get one decision right, and it is the decision C-26
exists to protect:

    "That's not what I said"   -> correct    the record was wrong. Belief ends;
                                             world validity is untouched, because a
                                             fact that was never true has no period
                                             to preserve.

    "She moved in March"       -> supersede  the world changed. Belief continues \u2014 we
                                             still think the old fact was true for its
                                             window \u2014 and world validity ends in March.

Both are reachable from a user saying "that's wrong", both preserve history, and
choosing wrong corrupts the timeline in a way that is very hard to detect later: the
records still look plausible, and only a `state_at` query against the wrong axis
reveals it. `services.md` Workflow 3 is explicit that this workflow **confirms rather
than infers when the signal is weak**, which is why the interrupt exists.

The second interrupt condition is scope. If a correction would touch more than one
memory, applying it to whichever ranked first is a guess about which record the user
meant, and a wrong guess silently retracts a fact they never mentioned.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from pca.domain.enums import CorrectionStatus, Granularity, OperationKind
from pca.domain.errors import MemoryNotFound
from pca.domain.ids import MemoryId
from pca.domain.memory import Fact
from pca.domain.orchestration import CorrectionRequest, CorrectionResult
from pca.domain.retrieval import RetrievalQuery
from pca.domain.temporal import (
    RelativeDescriptor,
    TemporalDirection,
    TemporalModifier,
    TemporalUnit,
)
from pca.observability.logging import get_logger
from pca.orchestration.checkpointer import PostgresCheckpointSaver
from pca.ports.clock import ClockPort
from pca.ports.llm import LLMProviderPort, Prompt, PromptMessage
from pca.ports.repositories import MemoryRepositoryPort
from pca.services.extraction import TimeReference
from pca.services.memory import MemoryService
from pca.services.retrieval import RetrievalService
from pca.services.time_resolver import TimeResolver

_log = get_logger(__name__)

# Below this the workflow asks instead of acting. Higher than the intent router's
# threshold on purpose: routing wrongly costs a wasted turn, correcting on the wrong
# axis costs the timeline.
CONFIDENCE_THRESHOLD = 0.75

_MAX_CANDIDATES = 5

_SYSTEM = """\
The user is telling you something you remembered is wrong or has changed. Decide
which of two operations applies. They are not interchangeable.

correct    The record itself was mistaken - you wrote down the wrong thing. The
           original was never true.
           "No, I said Bangalore, not Bengaluru East"

supersede  The record was right at the time, and the world has since changed. The
           original stays true for the period it covered.
           "She moved to Pune in March"

unclear    You genuinely cannot tell which. Choose this rather than guessing; the
           system will ask the user.

Also pick which remembered statement is being corrected, by index. If none of them
is plausibly the target, say so with a low confidence.

Report confidence honestly. Choosing the wrong operation damages the user's history
in a way neither of you will notice for a long time.
"""


def _keep_last(_existing: object, new: object) -> object:
    return new


class _Plan(BaseModel):
    operation: Literal["correct", "supersede", "unclear"]
    target_index: int = Field(description="0-based index into the listed statements")
    corrected_statement: str = Field(description="the statement as it should now read")
    when: TimeReference = Field(
        description="for supersede: when the change took effect. Contains no date."
    )
    confidence: float = Field(description="0.0 to 1.0, honestly reported")
    rationale: str = Field(description="one short clause")


class CorrectionState(TypedDict, total=False):
    request: CorrectionRequest
    candidates: Annotated[list[Fact], _keep_last]
    plan: Annotated[_Plan, _keep_last]
    confirmed_operation: Annotated[str, _keep_last]
    original_id: Annotated[MemoryId, _keep_last]
    replacement_id: Annotated[MemoryId, _keep_last]
    applied: Annotated[str, _keep_last]


class CorrectionWorkflow:
    def __init__(
        self,
        retrieval: RetrievalService,
        memory: MemoryService,
        memory_repository: MemoryRepositoryPort,
        provider: LLMProviderPort,
        clock: ClockPort,
        checkpointer: PostgresCheckpointSaver,
        resolver: TimeResolver | None = None,
        model: str | None = None,
    ) -> None:
        self._retrieval = retrieval
        self._memory = memory
        self._memory_repository = memory_repository
        self._provider = provider
        self._clock = clock
        self._resolver = resolver or TimeResolver()
        self._model = model
        self._graph = self._build(checkpointer)

    # ------------------------------------------------------------------- graph

    def _build(self, checkpointer: PostgresCheckpointSaver):  # type: ignore[no-untyped-def]
        builder = StateGraph(CorrectionState)
        builder.add_node("identify", self._identify)
        builder.add_node("plan", self._plan)
        builder.add_node("confirm", self._confirm)
        builder.add_node("apply", self._apply)

        builder.add_edge(START, "identify")
        builder.add_conditional_edges(
            "identify", self._nothing_found, {True: END, False: "plan"}
        )
        builder.add_conditional_edges(
            "plan", self._needs_confirmation, {True: "confirm", False: "apply"}
        )
        builder.add_edge("confirm", "apply")
        builder.add_edge("apply", END)

        # The checkpointer is what makes the confirmation interrupt survivable across
        # a restart. Without it this graph would still pause, but the pause would die
        # with the process and the user's answer would arrive at nothing.
        return builder.compile(checkpointer=checkpointer)

    @staticmethod
    def _nothing_found(state: CorrectionState) -> bool:
        return not state.get("candidates")

    def _needs_confirmation(self, state: CorrectionState) -> bool:
        plan = state["plan"]
        if plan.operation == "unclear" or plan.confidence < CONFIDENCE_THRESHOLD:
            return True
        # More than one plausible target means picking one is a guess about which
        # record the user meant, and a wrong guess retracts a fact they never
        # mentioned. `services.md` Workflow 3 calls this the "confirm scope" node.
        return len(state.get("candidates", [])) > 1

    # ------------------------------------------------------------------- nodes

    async def _identify(self, state: CorrectionState) -> CorrectionState:
        """Find which remembered statements the correction could be about."""
        request = state["request"]

        if request.memory_id is not None:
            fact = await self._memory_repository.get_fact(request.memory_id)
            return {"candidates": [fact] if fact else []}

        result = await self._retrieval.retrieve(
            RetrievalQuery(
                text=request.statement,
                budget=self._retrieval.budget_for("correct"),
            )
        )
        # Active only: correcting an already-retracted fact would open a second
        # belief window on a record nobody currently believes.
        return {
            "candidates": [f for f in result.facts if f.is_active][:_MAX_CANDIDATES]
        }

    async def _plan(self, state: CorrectionState) -> CorrectionState:
        request = state["request"]
        candidates = state["candidates"]
        listing = "\n".join(
            f"{i}. {fact.statement}" for i, fact in enumerate(candidates)
        )
        prompt = Prompt(
            system=_SYSTEM,
            messages=[
                PromptMessage(
                    role="user",
                    content=(
                        f"Remembered statements:\n{listing}\n\n"
                        f"What the user just said:\n{request.statement}"
                    ),
                )
            ],
            temperature=0.0,
        )
        try:
            plan = await self._provider.structured(prompt, _Plan, model=self._model)
        except Exception as exc:  # noqa: BLE001
            # Unlike conflict detection, this cannot proceed without the model — the
            # operation is the thing being decided. Fall through to the confirmation
            # interrupt rather than picking an axis by default.
            _log.warning("correction_planning_failed", error=str(exc)[:200])
            plan = _Plan(
                operation="unclear",
                target_index=0,
                corrected_statement=request.statement,
                when=TimeReference(raw_phrase="", kind="none"),
                confidence=0.0,
                rationale="planner unavailable",
            )
        return {"plan": plan}

    async def _confirm(self, state: CorrectionState) -> CorrectionState:
        """Pause and ask. Resumes with "correct" or "supersede".

        The graph stops here until someone answers, possibly after a process
        restart. Anything the user replies that is not recognised is treated as
        "correct", because it is the conservative option: correcting only ends a
        belief, whereas superseding asserts a change in the world that may not have
        happened.
        """
        plan = state["plan"]
        candidates = state["candidates"]
        answer = interrupt(
            {
                "question": (
                    "Did I record that wrongly, or has it changed since? "
                    "Reply 'wrong' or 'changed'."
                ),
                "statements": [fact.statement for fact in candidates],
                "proposed": plan.corrected_statement,
            }
        )
        normalised = str(answer).strip().casefold()
        operation = "supersede" if normalised in {"changed", "supersede"} else "correct"
        return {"confirmed_operation": operation}

    async def _apply(self, state: CorrectionState) -> CorrectionState:
        request = state["request"]
        plan = state["plan"]
        candidates = state["candidates"]

        index = plan.target_index if 0 <= plan.target_index < len(candidates) else 0
        target = candidates[index]
        operation = state.get("confirmed_operation") or plan.operation

        if operation == "supersede":
            outcome = await self._memory.supersede(
                target.id,
                new_statement=plan.corrected_statement,
                effective_from=self._effective_from(plan),
                reason=request.reason,
            )
            return {
                "original_id": outcome.original_id,
                "replacement_id": outcome.replacement_id,
                "applied": OperationKind.SUPERSEDE.value,
            }

        correction = await self._memory.correct(
            target.id,
            corrected_statement=plan.corrected_statement,
            reason=request.reason,
        )
        return {
            "original_id": correction.original_id,
            "replacement_id": correction.replacement_id,
            "applied": OperationKind.CORRECT.value,
        }

    # --------------------------------------------------------------- internals

    def _effective_from(self, plan: _Plan):  # type: ignore[no-untyped-def]
        """When the world change took effect.

        ADR-010's division of labour: the model produced a descriptor, this computes
        the date. An unresolvable phrase falls back to now rather than to a guessed
        date — the same choice the automatic supersession path makes with the
        utterance time. "It was true until today" is conservative and honest; a
        fabricated March would silently bound the old fact at a date nobody stated.
        """
        now = self._clock.now()
        reference = plan.when
        if reference.kind != "clock_relative":
            return now

        descriptor = RelativeDescriptor(
            direction=TemporalDirection(reference.direction),
            quantity=reference.quantity,
            unit=TemporalUnit(reference.unit) if reference.unit else None,
            weekday=reference.weekday,
            modifier=(
                TemporalModifier(reference.modifier) if reference.modifier else None
            ),
        )
        start, _end, granularity = self._resolver.resolve(
            descriptor, now, self._clock.zone()
        )
        if granularity is Granularity.UNKNOWN or start is None:
            _log.info(
                "correction_effective_from_unresolved",
                phrase=reference.raw_phrase,
                defaulted_to="now",
            )
            return now
        return start

    # ------------------------------------------------------------------ public

    async def run(self, request: CorrectionRequest) -> CorrectionResult:
        thread_id = f"correction:{uuid4()}"
        config = {"configurable": {"thread_id": thread_id}}
        final: CorrectionState = await self._graph.ainvoke(
            {"request": request}, config
        )
        return self._to_result(final, thread_id)

    async def resume(self, thread_id: str, answer: str) -> CorrectionResult:
        """Continue an interrupted correction with the user's answer."""
        config = {"configurable": {"thread_id": thread_id}}
        if await self._graph.aget_state(config) is None:
            raise MemoryNotFound(f"no correction in progress for {thread_id}")

        final: CorrectionState = await self._graph.ainvoke(
            Command(resume=answer), config
        )
        return self._to_result(final, thread_id)

    @staticmethod
    def _to_result(final: CorrectionState, thread_id: str) -> CorrectionResult:
        interrupts = final.get("__interrupt__")  # type: ignore[call-overload]
        if interrupts:
            payload = interrupts[0].value
            return CorrectionResult(
                status=CorrectionStatus.AWAITING_CONFIRMATION,
                thread_id=thread_id,
                question=payload.get("question"),
                options=list(payload.get("statements", [])),
            )

        if not final.get("candidates"):
            return CorrectionResult(
                status=CorrectionStatus.NOTHING_TO_CORRECT, thread_id=thread_id
            )

        return CorrectionResult(
            status=CorrectionStatus.APPLIED,
            thread_id=thread_id,
            operation=OperationKind(final["applied"]),
            original_id=final.get("original_id"),
            replacement_id=final.get("replacement_id"),
        )
