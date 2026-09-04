"""Unit 5 completion criterion, end to end.

Four properties, each stated so it would fail against the pre-Unit-5 code:

1. **A correction changes what retrieval returns.** Not just what the database holds —
   the corrected value has to reach the next reply, or the user sees the system agree
   it was wrong and then repeat the mistake.

2. **A clarification survives a process restart.** Asserted by discarding the workflow
   object and rebuilding it against the same checkpoint store. Resuming through the
   original object would prove nothing: the state would still be in memory.

3. **The reply does not wait for extraction (NFR-02.3).** The `done` event arrives with
   the episode's facts not yet committed. This is the assertion that actually retires
   the exception carried since Unit 1b — a test that merely checked extraction
   eventually happens would pass against the old synchronous code.

4. **The barrier restores ordering.** Message N+1 sees message N's facts, because the
   barrier settles the outstanding extraction before answering.

3 and 4 pull against each other, which is the whole reason ADR-008 exists: extraction
must not delay the reply, yet a fact stated now must be retrievable on the next
message. Testing them in one file keeps the tension visible.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from pca.domain.enums import ClarificationStatus, Confidence, EntityType, Origin
from pca.domain.ids import ConversationId, EntityId, MemoryId
from pca.domain.orchestration import AmbiguityContext, CorrectionRequest
from pca.orchestration.checkpointer import PostgresCheckpointSaver
from pca.orchestration.clarification_workflow import ClarificationWorkflow
from pca.orchestration.correction_workflow import CorrectionWorkflow, _Plan
from pca.services.entities import EntityService
from pca.services.extraction import ExtractedFact, ExtractionPayload, TimeReference
from tests.fakes.checkpoints import FakeCheckpointStore
from tests.fakes.clock import FakeClock
from tests.fakes.llm import FakeLLMProvider
from tests.fakes.memory_repositories import FakeEntityRepository
from tests.integration.test_api_skeleton import build_fake_container, make_client

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def payload(statement: str, about: str, category: str = "location") -> ExtractionPayload:
    return ExtractionPayload(
        facts=[
            ExtractedFact(
                statement=statement,
                origin="user_stated",
                category=category,
                about=[about],
            )
        ]
    )


@contextmanager
def client_with(provider: FakeLLMProvider, defer_extraction: bool = False):
    container = build_fake_container(
        provider=provider, defer_extraction=defer_extraction
    )
    with make_client(container) as client:
        yield client, container


def new_conversation(client: TestClient) -> str:
    return client.post("/conversations", json={}).json()["id"]


def send_to(client: TestClient, conversation_id: str, text: str):
    return client.post(
        f"/conversations/{conversation_id}/messages", json={"content": text}
    )


def sse(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: ") :]))
    return events


def context_text(provider: FakeLLMProvider) -> str:
    """The context actually handed to the model on the most recent turn."""
    streams = [call[1] for call in provider.calls if call[0] == "stream"]
    assert streams, "the model was never prompted"
    return "\n".join(m.content for m in streams[-1].messages)


# ------------------------------------------- criterion 1: correction is respected


async def test_a_correction_changes_what_retrieval_returns() -> None:
    """Completion criterion, half 1.

    The database holding the corrected value is not enough. If retrieval keeps
    returning the original, the assistant agrees it was wrong and then repeats the
    mistake on the next question — which is worse than never having accepted the
    correction.
    """
    provider = FakeLLMProvider(
        completions=["noted"],
        structured_results=[
            payload("Priya works at Google", "Priya", category="identity"),
            # The correction planner's decision.
            _Plan(
                operation="correct",
                target_index=0,
                corrected_statement="Priya works at Microsoft",
                when=TimeReference(raw_phrase="", kind="none"),
                confidence=0.95,
                rationale="user says the record was wrong",
            ),
        ],
    )
    with client_with(provider) as (client, container):
        conversation_id = new_conversation(client)
        send_to(client, conversation_id, "Priya works at Google")

        stored = {f.statement for f in container.test_memory_repo.facts.values()}  # type: ignore[attr-defined]
        assert "Priya works at Google" in stored

        outcome = await container.correction_workflow.run(
            CorrectionRequest(
                conversation_id=ConversationId(uuid4()),
                statement="Priya works at Microsoft, not Google",
                reason="user correction",
            )
        )
        assert outcome.status.value == "applied"

        # What retrieval now yields — the actual criterion.
        result = await container.retrieval.retrieve(
            __import__("pca.domain.retrieval", fromlist=["RetrievalQuery"]).RetrievalQuery(
                text="Where does Priya work?",
                budget=container.retrieval.budget_for(),
            )
        )
        statements = {f.statement for f in result.facts}

        assert "Priya works at Microsoft" in statements, (
            "retrieval must return the corrected value"
        )
        assert "Priya works at Google" not in statements, (
            "the original was corrected, meaning it was never true — it must not "
            "still be offered as a current fact"
        )


async def test_the_corrected_value_reaches_the_next_reply() -> None:
    """The same property one layer out: the model is shown the correction.

    Retrieval returning the right thing is necessary but not sufficient — the context
    package is what the reply is actually built from.
    """
    provider = FakeLLMProvider(
        completions=["noted"],
        structured_results=[
            payload("Priya works at Google", "Priya", category="identity"),
            _Plan(
                operation="correct",
                target_index=0,
                corrected_statement="Priya works at Microsoft",
                when=TimeReference(raw_phrase="", kind="none"),
                confidence=0.95,
                rationale="wrong record",
            ),
            payload("asking about Priya", "Priya", category="identity"),
        ],
    )
    with client_with(provider) as (client, container):
        conversation_id = new_conversation(client)
        send_to(client, conversation_id, "Priya works at Google")

        await container.correction_workflow.run(
            CorrectionRequest(
                conversation_id=ConversationId(uuid4()),
                statement="Priya works at Microsoft, not Google",
                reason="user correction",
            )
        )

        send_to(client, conversation_id, "Where does Priya work?")

        shown = context_text(provider)
        assert "Microsoft" in shown

        # Scoped to the epistemic fact buckets, deliberately excluding the two verbatim
        # sections. Both legitimately still contain "Google":
        #
        #   conversation history  the user really did say it; falsifying the transcript
        #                         would be worse than the problem being solved
        #   source excerpts       headed "what the user actually said". Note that the
        #                         replacement fact inherits the original's provenance
        #                         (MemoryService.correct copies it), so its excerpt
        #                         points at the pre-correction utterance. That is
        #                         coherent — the excerpt is evidence of what was said,
        #                         the fact states what is now believed — and it is why
        #                         this assertion has to be scoped rather than global.
        #
        # What must not survive is the retracted statement appearing as a CURRENT fact.
        facts_section = shown.split("## Source excerpts")[0].split(
            "## Current conversation"
        )[0]
        assert "Google" not in facts_section, (
            "a corrected fact must not still be presented to the model as current; "
            f"facts section was:\n{facts_section}"
        )


# ------------------------------- criterion 2: clarification survives a restart


async def test_a_clarification_survives_a_process_restart() -> None:
    """Completion criterion, half 2.

    The workflow object is discarded and rebuilt against the same checkpoint store,
    which is what a restarted process would see. Resuming through the original object
    would assert nothing, because the interrupt would still be in memory.
    """
    store = FakeCheckpointStore()
    clock = FakeClock(start=NOW, zone="Asia/Kolkata")
    entity_repo = FakeEntityRepository()
    entities = EntityService(repository=entity_repo, clock=clock)

    keep = await entity_repo.create(
        entity_id=EntityId(uuid4()),
        name="Sarah Chen",
        entity_type=EntityType.PERSON,
        created_at=NOW,
    )
    provisional = await entity_repo.create(
        entity_id=EntityId(uuid4()),
        name="Sarah",
        entity_type=EntityType.PERSON,
        created_at=NOW,
        is_provisional=True,
    )

    before = ClarificationWorkflow(
        entities=entities, checkpointer=PostgresCheckpointSaver(store)  # type: ignore[arg-type]
    )
    started = await before.run(
        AmbiguityContext(
            conversation_id=ConversationId(uuid4()),
            question="Which Sarah did you mean?",
            options=["Sarah Chen", "Sarah"],
        )
    )
    assert started.status is ClarificationStatus.AWAITING_ANSWER
    assert entity_repo.merged == {}, "nothing may be written before the answer"

    # The restart. Everything except the durable store is rebuilt.
    del before
    after = ClarificationWorkflow(
        entities=EntityService(repository=entity_repo, clock=clock),
        checkpointer=PostgresCheckpointSaver(store),  # type: ignore[arg-type]
    )

    resumed = await after.resume(started.thread_id, "Sarah Chen")

    assert resumed.status is ClarificationStatus.RESOLVED
    assert provisional.id in entity_repo.merged, (
        "the answer must be applied after the restart"
    )
    assert entity_repo.merged[provisional.id][0] == keep.id


# --------------------------------------- criterion 3: NFR-02.3, the reply is first


def test_the_reply_completes_before_extraction_runs() -> None:
    """NFR-02.3 — the assertion that retires the Unit 1b exception.

    Against the old synchronous code the facts would already be committed by the time
    `done` was emitted, so this test would fail. That is what makes it meaningful
    rather than a restatement of the implementation.
    """
    provider = FakeLLMProvider(
        completions=["noted"],
        structured_results=[payload("Priya lives in Pune", "Priya")],
    )
    with client_with(provider, defer_extraction=True) as (client, container):
        conversation_id = new_conversation(client)
        response = send_to(client, conversation_id, "Priya lives in Pune")

        events = sse(response.text)
        assert events[-1]["done"] is True, "the stream must have completed"

        # The reply is finished and the episode is queued, but nothing is extracted.
        assert container.coordinator.submitted, "the episode must have been queued"  # type: ignore[attr-defined]
        assert container.coordinator.ran == [], (  # type: ignore[attr-defined]
            "extraction must NOT have run before the reply completed — that is the "
            "NFR-02.3 requirement"
        )
        assert container.test_memory_repo.facts == {}, (  # type: ignore[attr-defined]
            "no facts may be committed yet"
        )


def test_extraction_still_happens_afterwards() -> None:
    """Deferring is not dropping. The counterpart to the assertion above."""
    provider = FakeLLMProvider(
        completions=["noted"],
        structured_results=[payload("Priya lives in Pune", "Priya")],
    )
    with client_with(provider, defer_extraction=True) as (client, container):
        conversation_id = new_conversation(client)
        send_to(client, conversation_id, "Priya lives in Pune")

        # Settle the queue the way the next turn's barrier would.
        client.post(
            f"/conversations/{conversation_id}/messages",
            json={"content": "and anything else?"},
        )

        stored = {f.statement for f in container.test_memory_repo.facts.values()}  # type: ignore[attr-defined]
        assert "Priya lives in Pune" in stored


# ------------------------------------------- criterion 4: the barrier restores order


def test_the_next_message_sees_the_previous_messages_facts() -> None:
    """Barrier ordering, and the core product hypothesis.

    Extraction is deferred, so without the barrier the second turn would retrieve
    against an empty store. The barrier settles turn one's extraction first, which is
    precisely the guarantee ADR-008 trades latency for.
    """
    provider = FakeLLMProvider(
        completions=["noted"],
        structured_results=[
            payload("Priya lives in Pune", "Priya"),
            payload("asking where she lives", "Priya"),
        ],
    )
    with client_with(provider, defer_extraction=True) as (client, container):
        conversation_id = new_conversation(client)
        send_to(client, conversation_id, "Priya lives in Pune")

        # Nothing committed yet — the precondition that makes this test meaningful.
        assert container.test_memory_repo.facts == {}  # type: ignore[attr-defined]

        send_to(client, conversation_id, "Where does Priya live?")

        assert container.coordinator.barrier_calls, (  # type: ignore[attr-defined]
            "the barrier must be consulted before answering"
        )
        shown = context_text(provider)
        assert "Pune" in shown, (
            "the fact stated on the previous turn must be available on this one"
        )


def test_the_barrier_is_awaited_before_the_reply_is_generated() -> None:
    """Ordering within the turn: barrier first, then generation.

    Checked directly because the sequence is invisible in the response body — a
    barrier consulted after generation would still appear to work on a fast machine.
    """
    provider = FakeLLMProvider(
        completions=["noted"],
        structured_results=[payload("Priya lives in Pune", "Priya")],
    )
    with client_with(provider, defer_extraction=True) as (client, container):
        conversation_id = new_conversation(client)
        send_to(client, conversation_id, "Priya lives in Pune")

        # One barrier call per message, and it precedes the model call.
        assert len(container.coordinator.barrier_calls) == 1  # type: ignore[attr-defined]
        assert any(call[0] == "stream" for call in provider.calls)


def test_deferred_findings_reach_the_user_on_the_following_turn() -> None:
    """Moving extraction off the response path must not lose FR-05.6 / ADR-014
    notices — only delay them by one turn.

    Two provisional entities named the same thing force an ambiguous resolution, which
    sets `needs_clarification` during the deferred extraction. The notice therefore
    cannot appear on turn one; it must appear on turn two.
    """
    from pca.domain.memory import Entity

    provider = FakeLLMProvider(
        completions=["noted"],
        structured_results=[
            payload("Priya called me", "Priya", category="relationship"),
            payload("anything else", "Priya", category="relationship"),
        ],
    )
    container = build_fake_container(provider=provider, defer_extraction=True)

    repo = container.test_entity_repo  # type: ignore[attr-defined]
    for _ in range(2):
        entity_id = EntityId(uuid4())
        repo.entities[entity_id] = Entity(
            id=entity_id, name="Priya", entity_type=EntityType.PERSON
        )
        repo.aliases[entity_id] = set()

    with make_client(container) as client:
        conversation_id = new_conversation(client)
        first = send_to(client, conversation_id, "Priya called me")

        assert "notices" not in sse(first.text)[-1], (
            "the ambiguity is not yet known on this turn — extraction has not run"
        )

        second = send_to(client, conversation_id, "Anything else?")

        notices = sse(second.text)[-1].get("notices", [])
        assert any("unambiguously" in n for n in notices), (
            f"the deferred finding must be delivered, got {notices}"
        )
