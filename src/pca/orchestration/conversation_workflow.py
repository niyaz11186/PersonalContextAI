"""ConversationWorkflow — the normal path.

Layer L2. **The only layer permitted to import `langgraph`** (boundary rule 2).

ADR-006 confines LangGraph to orchestration and keeps the graph thin: every node
body is a call into an L3 service plus state mapping, and contains no business
logic. That is what keeps the exit option in ADR-006 live — if LangGraph proves to
be the wrong choice, replacing it means rewriting this file and nothing else.

Unit 5 wires two things onto this path that previously existed but were unreachable:

**Intent classification.** The budget for retrieval now depends on what the user is
actually doing (D-4). Routing itself happens at the dispatch point in the API rather
than here — a destination workflow is the wrong place to decide whether it should have
been the destination — but a decision made there is passed in and reused, so the
classification costs exactly one model call per turn either way.

**Degradation with disclosure.** Retrieval failure used to propagate and fail the
request. It now applies `DegradationPolicy.on_retrieval_failure` and answers from the
conversation alone, carrying the disclosure into the context package. The one exception
is PostgreSQL: constraint C-22 gives the system of record no degradation path, because
an answer assembled without it would come from the store ADR-015 designates
non-authoritative. That failure still raises.

Deliberately NOT checkpointed. The plan called for attaching the Step 5 checkpointer
here, and on inspection that is cost without a reader: this graph has no interrupt, so
there is nothing to resume, and a checkpoint per conversation turn would be durable
writes nothing reads. The same reasoning was applied to `HistoricalAnalysisWorkflow`.
`ClarificationWorkflow` is where the dependency earns itself. If a later unit
introduces a mid-conversation interrupt, attaching it is a one-line change.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from pca.domain.conversation import Message
from pca.domain.errors import SourceOfRecordUnavailable
from pca.domain.ids import ConversationId
from pca.domain.orchestration import Degradation, RoutingDecision
from pca.domain.retrieval import (
    ContextPackage,
    RetrievalDiagnostics,
    RetrievalQuery,
    RetrievalResult,
)
from pca.observability.logging import get_logger
from pca.orchestration.intent_router import IntentRouter
from pca.ports.llm import LLMProviderPort, Prompt, PromptMessage
from pca.services.context_assembly import ContextAssemblyService
from pca.services.conversation import ConversationService
from pca.services.degradation import DegradationPolicy
from pca.services.retrieval import RetrievalService

_log = get_logger(__name__)

_SYSTEM_PROMPT = """\
You are a private personal-context assistant. You hold the user's own history.

How to use the context you are given:
- Material under "Stated by the user" is fact. You may rely on it.
- Material under "Current state" is true now but replaced an earlier record. If
  asked about the past, say the record changed rather than implying it always held.
- Material under "Derived by the system" is your own earlier inference. Attribute
  it as such if you use it; never present it as something the user told you.
- Material under "Uncertain" must not be asserted. Ask instead.
- If records conflict, say so and show both. Do not silently pick one.
- If the context does not contain what the question needs, say you do not know.
  Never fill a gap with a plausible recollection.

Be direct and warm. Do not be sycophantic.
"""


def _keep_last(_existing: object, new: object) -> object:
    """Reducer: last write wins. Nodes here each own distinct keys."""
    return new


class ConversationState(TypedDict, total=False):
    conversation_id: ConversationId
    user_message: str
    has_open_clarification: bool
    routing: Annotated[RoutingDecision, _keep_last]
    history: Annotated[Sequence[Message], _keep_last]
    retrieval: Annotated[RetrievalResult, _keep_last]
    degradation: Annotated[Degradation | None, _keep_last]
    context: Annotated[ContextPackage, _keep_last]
    rendered_context: Annotated[str, _keep_last]


class ConversationWorkflow:
    def __init__(
        self,
        conversations: ConversationService,
        retrieval: RetrievalService,
        assembly: ContextAssemblyService,
        provider: LLMProviderPort,
        router: IntentRouter | None = None,
        degradation: DegradationPolicy | None = None,
        model: str | None = None,
    ) -> None:
        self._conversations = conversations
        self._retrieval = retrieval
        self._assembly = assembly
        self._provider = provider
        self._router = router
        # Defaulted rather than injected-or-nothing because the policy is pure with no
        # dependencies, so constructing one here cannot silently skip behaviour. The
        # router is left optional-but-explicit: without one, classification is skipped
        # and the default budget applies, which is the pre-Unit-5 behaviour.
        self._degradation = degradation or DegradationPolicy()
        self._model = model
        self._graph = self._build()

    # ------------------------------------------------------------------- graph

    def _build(self):  # type: ignore[no-untyped-def]
        builder = StateGraph(ConversationState)
        builder.add_node("classify", self._classify)
        builder.add_node("load_history", self._load_history)
        builder.add_node("retrieve", self._retrieve)
        builder.add_node("assemble", self._assemble)

        builder.add_edge(START, "classify")
        builder.add_edge("classify", "load_history")
        builder.add_edge("load_history", "retrieve")
        builder.add_edge("retrieve", "assemble")
        builder.add_edge("assemble", END)

        # See the module docstring: no checkpointer, because there is no interrupt on
        # this path and per-turn checkpoints would be writes with no reader.
        return builder.compile()

    # ------------------------------------------------------------------- nodes

    async def _classify(self, state: ConversationState) -> ConversationState:
        """Establish the intent, without paying for it twice.

        A decision already supplied by the caller wins. The API classifies in order to
        route, so re-classifying here would spend a second model call on the response
        path to reach the same answer (D-4 is explicit that this classification sits in
        the latency budget).
        """
        existing = state.get("routing")
        if existing is not None:
            return {"routing": existing}

        if self._router is None:
            return {}

        decision = await self._router.classify(
            state["user_message"],
            state["conversation_id"],
            has_open_clarification=state.get("has_open_clarification", False),
        )
        return {"routing": decision}

    async def _load_history(self, state: ConversationState) -> ConversationState:
        history = await self._conversations.get_history(state["conversation_id"])
        return {"history": history}

    async def _retrieve(self, state: ConversationState) -> ConversationState:
        """Fetch context, degrading rather than failing (NFR-06.5).

        `RetrievalService` already isolates per-strategy failures internally, so what
        reaches this handler is a failure of the whole stage — most plausibly the
        memory repository. That is worth degrading on rather than returning a 500 for:
        the conversation itself is still answerable, just with less.
        """
        routing = state.get("routing")
        intent = routing.intent.value if routing is not None else None

        query = RetrievalQuery(
            text=state["user_message"],
            budget=self._retrieval.budget_for(intent),
        )
        try:
            result = await self._retrieval.retrieve(query)
        except SourceOfRecordUnavailable:
            # C-22: PostgreSQL has no degradation path. Answering without it would
            # mean answering from Neo4j alone, which ADR-015 designates
            # non-authoritative — a confidently wrong answer rather than a missing one.
            raise
        except Exception as exc:  # noqa: BLE001 - degradation is the point
            degradation = self._degradation.on_retrieval_failure(exc)
            return {
                "retrieval": RetrievalResult(
                    diagnostics=RetrievalDiagnostics(
                        degraded=True,
                        notes=[f"retrieval stage failed: {type(exc).__name__}"],
                    )
                ),
                "degradation": degradation,
            }

        return {"retrieval": result, "degradation": None}

    async def _assemble(self, state: ConversationState) -> ConversationState:
        package = await self._assembly.assemble(
            result=state["retrieval"],
            history=state.get("history", []),
        )

        # The policy's disclosure is merged into the package rather than logged and
        # dropped. `Degradation` binds action to disclosure precisely so that acting on
        # one without showing the other takes deliberate effort (C-34).
        degradation = state.get("degradation")
        if degradation is not None:
            package = replace(
                package,
                degradation_notices=[
                    *package.degradation_notices,
                    degradation.disclosure,
                ],
            )

        rendered = self._assembly.render(package)
        return {"context": package, "rendered_context": rendered}

    # -------------------------------------------------------------------- public

    async def run(
        self,
        conversation_id: ConversationId,
        user_message: str,
        routing: RoutingDecision | None = None,
        has_open_clarification: bool = False,
    ) -> AsyncIterator[str]:
        """Run the graph, then stream the reply.

        `routing` is accepted so the API's dispatch decision is reused rather than
        recomputed — see `_classify`.

        Generation sits outside the graph. LangGraph's streaming surface is worth
        adopting once several workflows share it; for a single linear path it would be
        indirection without benefit.
        """
        final: ConversationState = await self._graph.ainvoke(
            self._initial(conversation_id, user_message, routing, has_open_clarification)
        )

        rendered = final.get("rendered_context", "")
        decision = final.get("routing")
        _log.info(
            "conversation_context_ready",
            conversation_id=str(conversation_id),
            context_chars=len(rendered),
            intent=decision.intent.value if decision is not None else None,
            confidence=round(decision.confidence, 2) if decision is not None else None,
            degraded=bool(final.get("context") and final["context"].degradation_notices),
        )

        prompt = Prompt(
            system=_SYSTEM_PROMPT,
            messages=[
                PromptMessage(role="user", content=f"# Context\n\n{rendered}"),
                PromptMessage(role="user", content=user_message),
            ],
            temperature=0.4,
        )

        async for token in self._provider.stream(prompt, model=self._model):
            yield token

    async def build_context(
        self,
        conversation_id: ConversationId,
        user_message: str,
        routing: RoutingDecision | None = None,
        has_open_clarification: bool = False,
    ) -> ContextPackage:
        """Run the graph without generating.

        Exposed for inspection and for tests that assert on context construction
        rather than on model output.
        """
        final: ConversationState = await self._graph.ainvoke(
            self._initial(conversation_id, user_message, routing, has_open_clarification)
        )
        return final["context"]

    @staticmethod
    def _initial(
        conversation_id: ConversationId,
        user_message: str,
        routing: RoutingDecision | None,
        has_open_clarification: bool,
    ) -> ConversationState:
        state: ConversationState = {
            "conversation_id": conversation_id,
            "user_message": user_message,
            "has_open_clarification": has_open_clarification,
        }
        if routing is not None:
            state["routing"] = routing
        return state
