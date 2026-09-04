"""Unit 5 Step 12 — the conversation path's new wiring.

Two behaviours were specified during Inception and unreachable until now:

**Retrieval failure degrades with disclosure (NFR-06.5).** It previously propagated
and failed the request. The disclosure is the part that actually matters: a reply built
without memory that reads exactly like a healthy one leads the user to conclude they
never mentioned something, which is precisely wrong.

**PostgreSQL does not degrade (C-22).** The one exception, asserted separately, because
a policy that degrades on everything would quietly answer from the store ADR-015
designates non-authoritative.

The intent classification tests check the call is made *once*. D-4 put this
classification inside the latency budget on the assumption of a single call; a
destination workflow that re-classifies what the dispatcher already decided doubles it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from pca.domain.conversation import Message
from pca.domain.enums import Intent, Role
from pca.domain.errors import MemoryGraphUnavailable, SourceOfRecordUnavailable
from pca.domain.ids import ConversationId, MessageId
from pca.domain.orchestration import RoutingDecision
from pca.domain.retrieval import RetrievalDiagnostics, RetrievalResult
from pca.orchestration.conversation_workflow import ConversationWorkflow
from pca.services.context_assembly import ContextAssemblyService
from pca.services.degradation import DegradationPolicy

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class StubConversations:
    def __init__(self, history: list[Message] | None = None) -> None:
        self._history = history or []

    async def get_history(self, conversation_id: ConversationId, limit=None):  # type: ignore[no-untyped-def]
        return self._history


class StubRetrieval:
    """Retrieval that can be made to fail in a chosen way."""

    def __init__(self, raise_with: Exception | None = None) -> None:
        self._raise = raise_with
        self.budgets_requested: list[str | None] = []
        self.calls = 0

    def budget_for(self, intent: str | None = None):  # type: ignore[no-untyped-def]
        self.budgets_requested.append(intent)
        from pca.services.budget import DEFAULT_BUDGET

        return DEFAULT_BUDGET

    async def retrieve(self, query):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        return RetrievalResult(diagnostics=RetrievalDiagnostics())


class SpyRouter:
    def __init__(self, intent: Intent = Intent.CONVERSE) -> None:
        self.intent = intent
        self.calls = 0

    async def classify(
        self, message: str, conversation_id: ConversationId, has_open_clarification: bool = False
    ) -> RoutingDecision:
        self.calls += 1
        return RoutingDecision(
            intent=self.intent, confidence=0.9, rationale="stub"
        )


class StubProvider:
    async def stream(self, prompt, *, model=None):  # type: ignore[no-untyped-def]
        yield "ok"


def build(
    *,
    retrieval_raises: Exception | None = None,
    router: SpyRouter | None = None,
    history: list[Message] | None = None,
) -> tuple[ConversationWorkflow, StubRetrieval, SpyRouter | None]:
    retrieval = StubRetrieval(raise_with=retrieval_raises)
    workflow = ConversationWorkflow(
        conversations=StubConversations(history),  # type: ignore[arg-type]
        retrieval=retrieval,  # type: ignore[arg-type]
        assembly=ContextAssemblyService(),
        provider=StubProvider(),  # type: ignore[arg-type]
        router=router,  # type: ignore[arg-type]
        degradation=DegradationPolicy(),
    )
    return workflow, retrieval, router


def message(text: str) -> Message:
    return Message(
        id=MessageId(uuid4()),
        conversation_id=ConversationId(uuid4()),
        role=Role.USER,
        content=text,
        captured_at=NOW,
        zone="UTC",
    )


# ------------------------------------------------------------------ degradation


async def test_retrieval_failure_degrades_instead_of_failing() -> None:
    """Previously this propagated and the request returned an error."""
    workflow, _, _ = build(retrieval_raises=MemoryGraphUnavailable("neo4j down"))

    package = await workflow.build_context(ConversationId(uuid4()), "where does she live?")

    assert package.degradation_notices, (
        "a degraded answer must carry disclosure (NFR-06.5)"
    )


async def test_the_disclosure_reaches_the_rendered_prompt() -> None:
    """Carrying the notice in the package is not enough — it has to survive render.

    If it stopped at the package boundary the model would never see it, and the reply
    would read as though memory had been consulted successfully.
    """
    workflow, _, _ = build(retrieval_raises=MemoryGraphUnavailable("neo4j down"))
    conversation_id = ConversationId(uuid4())

    package = await workflow.build_context(conversation_id, "where does she live?")
    rendered = ContextAssemblyService().render(package)

    assert "could not reach my memory" in rendered
    assert "Reliability notice" in rendered


async def test_the_disclosure_is_the_policys_text_not_an_inline_literal() -> None:
    """The point of DegradationPolicy is that these sentences live in one place.

    Asserting against the policy's own output rather than a copied string means the
    test cannot pass while the workflow quietly reintroduces its own wording.
    """
    expected = DegradationPolicy().on_retrieval_failure(
        MemoryGraphUnavailable("x")
    ).disclosure

    workflow, _, _ = build(retrieval_raises=MemoryGraphUnavailable("x"))
    package = await workflow.build_context(ConversationId(uuid4()), "q")

    assert expected in package.degradation_notices


async def test_conversation_history_still_reaches_the_model_when_memory_fails() -> None:
    """"Proceed without memory" must still mean proceed. An empty context would be a
    failure wearing a disclosure."""
    history = [message("Priya lives in Pune")]
    workflow, _, _ = build(
        retrieval_raises=MemoryGraphUnavailable("down"), history=history
    )

    package = await workflow.build_context(ConversationId(uuid4()), "where?")

    assert package.conversation_history, (
        "the conversation itself is still available and must be used"
    )


async def test_postgres_failure_is_not_degraded(caplog: pytest.LogCaptureFixture) -> None:
    """C-22: the system of record has no degradation path.

    Answering without PostgreSQL means answering from Neo4j alone — the store ADR-015
    designates non-authoritative. A confidently wrong answer is worse than an error.
    """
    workflow, _, _ = build(
        retrieval_raises=SourceOfRecordUnavailable("postgres down")
    )

    with pytest.raises(SourceOfRecordUnavailable):
        await workflow.build_context(ConversationId(uuid4()), "q")


async def test_a_healthy_retrieval_adds_no_notices() -> None:
    """The disclosure must not appear on the happy path, or it becomes noise the user
    learns to ignore."""
    workflow, _, _ = build()

    package = await workflow.build_context(ConversationId(uuid4()), "q")

    assert package.degradation_notices == []


# --------------------------------------------------------------- intent wiring


async def test_the_intent_shapes_the_retrieval_budget() -> None:
    """D-4's purpose: the budget depends on what the user is doing."""
    router = SpyRouter(intent=Intent.HISTORICAL)
    workflow, retrieval, _ = build(router=router)

    await workflow.build_context(ConversationId(uuid4()), "what changed in March?")

    assert retrieval.budgets_requested == ["historical"], (
        "the classified intent must reach budget_for"
    )


async def test_a_supplied_decision_is_reused_rather_than_reclassified() -> None:
    """The API classifies to route. Re-classifying here would spend a second model
    call on the response path to reach an answer we already have."""
    router = SpyRouter()
    workflow, retrieval, _ = build(router=router)
    decision = RoutingDecision(
        intent=Intent.CORRECT, confidence=0.95, rationale="from the dispatcher"
    )

    await workflow.build_context(
        ConversationId(uuid4()), "no, Bangalore", routing=decision
    )

    assert router.calls == 0, "must not re-classify a decision the caller supplied"
    assert retrieval.budgets_requested == ["correct"]


async def test_classification_happens_once_when_not_supplied() -> None:
    router = SpyRouter()
    workflow, _, _ = build(router=router)

    await workflow.build_context(ConversationId(uuid4()), "hello")

    assert router.calls == 1


async def test_an_open_clarification_is_passed_to_the_router() -> None:
    """D-3's in-band half: when a question is outstanding, the next message is
    overwhelmingly the answer to it."""

    class RecordingRouter(SpyRouter):
        def __init__(self) -> None:
            super().__init__()
            self.saw_open_clarification: bool | None = None

        async def classify(  # type: ignore[override]
            self,
            message: str,
            conversation_id: ConversationId,
            has_open_clarification: bool = False,
        ) -> RoutingDecision:
            self.saw_open_clarification = has_open_clarification
            return await super().classify(
                message, conversation_id, has_open_clarification
            )

    router = RecordingRouter()
    workflow, _, _ = build(router=router)

    await workflow.build_context(
        ConversationId(uuid4()), "the one from work", has_open_clarification=True
    )

    assert router.saw_open_clarification is True


async def test_no_router_falls_back_to_the_default_budget() -> None:
    """Absence of a router must not crash the path — it degrades to pre-Unit-5
    behaviour, which is a working conversation with an unspecialised budget."""
    workflow, retrieval, _ = build(router=None)

    package = await workflow.build_context(ConversationId(uuid4()), "hello")

    assert retrieval.budgets_requested == [None]
    assert package is not None


async def test_streaming_still_works_through_the_rewired_graph() -> None:
    """The classify node sits at START, so a mistake there breaks every reply."""
    router = SpyRouter()
    workflow, _, _ = build(router=router)

    tokens = [
        token
        async for token in workflow.run(ConversationId(uuid4()), "hello")
    ]

    assert "".join(tokens) == "ok"
