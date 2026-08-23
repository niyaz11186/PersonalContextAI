"""ConversationWorkflow — the normal path.

Layer L2. **The only layer permitted to import `langgraph`** (boundary rule 2).

ADR-006 confines LangGraph to orchestration and keeps the graph thin: every node
body is a call into an L3 service plus state mapping, and contains no business
logic. That is what keeps the exit option in ADR-006 live — if LangGraph proves to
be the wrong choice, replacing it means rewriting this file and nothing else.

Unit scope: retrieve, assemble, generate. Unit 5 adds the extraction, correction,
historical-analysis, and clarification workflows, and the interrupt/resume
machinery that is the main justification for the dependency.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from pca.domain.conversation import Message
from pca.domain.ids import ConversationId
from pca.domain.retrieval import ContextPackage, RetrievalQuery, RetrievalResult
from pca.observability.logging import get_logger
from pca.ports.graph import GraphHit
from pca.ports.llm import LLMProviderPort, Prompt, PromptMessage
from pca.services.context_assembly import ContextAssemblyService
from pca.services.conversation import ConversationService
from pca.services.retrieval import DEFAULT_BUDGET, RetrievalService

_log = get_logger(__name__)

_SYSTEM_PROMPT = """\
You are a private personal-context assistant. You hold the user's own history.

How to use the context you are given:
- Material under "Stated by the user" is fact. You may rely on it.
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
    history: Annotated[Sequence[Message], _keep_last]
    retrieval: Annotated[RetrievalResult, _keep_last]
    raw_hits: Annotated[Sequence[GraphHit], _keep_last]
    context: Annotated[ContextPackage, _keep_last]
    rendered_context: Annotated[str, _keep_last]


class ConversationWorkflow:
    def __init__(
        self,
        conversations: ConversationService,
        retrieval: RetrievalService,
        assembly: ContextAssemblyService,
        provider: LLMProviderPort,
        model: str | None = None,
    ) -> None:
        self._conversations = conversations
        self._retrieval = retrieval
        self._assembly = assembly
        self._provider = provider
        self._model = model
        self._graph = self._build()

    # ------------------------------------------------------------------- graph

    def _build(self):  # type: ignore[no-untyped-def]
        builder = StateGraph(ConversationState)
        builder.add_node("load_history", self._load_history)
        builder.add_node("retrieve", self._retrieve)
        builder.add_node("assemble", self._assemble)

        builder.add_edge(START, "load_history")
        builder.add_edge("load_history", "retrieve")
        builder.add_edge("retrieve", "assemble")
        builder.add_edge("assemble", END)

        # No checkpointer in the skeleton: this path has no interrupt, so there is
        # nothing to resume. Unit 5 attaches the PostgreSQL checkpointer when
        # ClarificationWorkflow introduces a genuine pause.
        return builder.compile()

    # ------------------------------------------------------------------- nodes

    async def _load_history(self, state: ConversationState) -> ConversationState:
        history = await self._conversations.get_history(state["conversation_id"])
        return {"history": history}

    async def _retrieve(self, state: ConversationState) -> ConversationState:
        query = RetrievalQuery(text=state["user_message"], budget=DEFAULT_BUDGET)
        result = await self._retrieval.retrieve(query)

        # Naive-only: the skeleton has no typed memory yet, so raw graph hits are
        # carried through to give the model something. Unit 3 replaces this with
        # committed Facts and Events.
        try:
            raw = await self._retrieval.raw_hits(
                state["user_message"], limit=DEFAULT_BUDGET.max_items
            )
        except Exception:  # noqa: BLE001 - degradation already recorded upstream
            raw = []

        return {"retrieval": result, "raw_hits": raw}

    async def _assemble(self, state: ConversationState) -> ConversationState:
        package = await self._assembly.assemble(
            result=state["retrieval"],
            history=state.get("history", []),
            raw_hits=state.get("raw_hits", []),
        )
        rendered = self._assembly.render(package, raw_hits=state.get("raw_hits", []))
        return {"context": package, "rendered_context": rendered}

    # -------------------------------------------------------------------- public

    async def run(
        self, conversation_id: ConversationId, user_message: str
    ) -> AsyncIterator[str]:
        """Run the graph, then stream the reply.

        Generation sits outside the graph in this unit. LangGraph's streaming
        surface is worth adopting once there are several workflows sharing it
        (Unit 5); doing it now would add indirection for a single linear path.
        """
        final: ConversationState = await self._graph.ainvoke(
            {"conversation_id": conversation_id, "user_message": user_message}
        )

        rendered = final.get("rendered_context", "")
        _log.info(
            "conversation_context_ready",
            conversation_id=str(conversation_id),
            context_chars=len(rendered),
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
        self, conversation_id: ConversationId, user_message: str
    ) -> ContextPackage:
        """Run the graph without generating.

        Exposed for inspection and for tests that assert on context construction
        rather than on model output.
        """
        final: ConversationState = await self._graph.ainvoke(
            {"conversation_id": conversation_id, "user_message": user_message}
        )
        return final["context"]
