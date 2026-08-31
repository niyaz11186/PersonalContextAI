"""Unit 4 through the HTTP flow.

The unit tests prove the retrieval logic. These prove the pipeline is CONNECTED —
that a question requiring history actually produces a bounded, labelled context
package on the request path, and that diagnostics travel with it.

Worth having separately: `RetrievalService` and `ContextAssemblyService` could both be
correct while the workflow still passed an empty package to the model, which is very
close to what Unit 1b did.
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from pca.domain.enums import Confidence, Origin
from pca.domain.ids import MemoryId
from pca.services.extraction import ExtractedFact, ExtractionPayload
from tests.fakes.llm import FakeLLMProvider
from tests.integration.test_api_skeleton import build_fake_container, make_client


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
def client_with(provider: FakeLLMProvider):
    """Entered TestClient plus the container holding the fakes.

    Must be entered as a context manager or the lifespan never runs and
    `app.state.container` is never populated.
    """
    container = build_fake_container(provider=provider)
    with make_client(container) as client:
        yield client, container


def send(client: TestClient, text: str) -> object:
    conversation_id = client.post("/conversations", json={}).json()["id"]
    return client.post(
        f"/conversations/{conversation_id}/messages", json={"content": text}
    )


def test_a_question_requiring_history_gets_a_labelled_context_package() -> None:
    """The completion criterion, end to end.

    First message commits a fact. Second message asks about it in a separate
    conversation, so the answer can only come from stored memory.
    """
    provider = FakeLLMProvider(
        completions=["noted"],
        structured_results=[
            payload("Priya lives in Pune", "Priya"),
            payload("asking about Priya", "Priya"),
        ],
    )
    with client_with(provider) as (client, container):
        send(client, "Priya lives in Pune")

        # Second conversation: nothing in local history, so any recall must come
        # from retrieval.
        response = send(client, "Where does Priya live?")
        assert response.status_code == 200

        rendered_prompts = [
            call[1] for call in provider.calls if call[0] == "stream"
        ]
        assert rendered_prompts, "the model should have been prompted"

        # The context handed to the model must carry the stored fact under an
        # epistemic heading, not as unlabelled text.
        last = rendered_prompts[-1]
        context_text = "\n".join(m.content for m in last.messages)
        assert "Priya lives in Pune" in context_text
        assert "Stated by the user" in context_text


def test_diagnostics_report_which_strategies_contributed() -> None:
    """Second half of the completion criterion.

    Asserted on the SECOND message. Retrieval runs before commit on the request path,
    so the first message always retrieves against an empty store — every strategy
    runs, finds nothing, and contributes nothing. That is a legitimate third state
    alongside "contributed" and "failed", and asserting contribution on a cold start
    would be asserting something the design does not promise.
    """
    provider = FakeLLMProvider(
        completions=["noted"],
        structured_results=[
            payload("Priya lives in Pune", "Priya"),
            payload("asking about Priya", "Priya"),
        ],
    )
    with client_with(provider) as (client, container):
        send(client, "Priya lives in Pune")

        cold = container.test_last_retrieval  # type: ignore[attr-defined]
        assert cold.diagnostics.spends, "every strategy that ran must be recorded"
        assert cold.diagnostics.contributing_strategies == [], (
            "nothing was stored yet, so nothing can have contributed"
        )

        send(client, "Priya lives in Pune")

        warm = container.test_last_retrieval  # type: ignore[attr-defined]
        assert warm.diagnostics.contributing_strategies, (
            "with a fact stored and indexed, at least one strategy must contribute"
        )
        assert {s.strategy for s in warm.diagnostics.spends} >= {
            "semantic",
            "fulltext",
        }, "per-strategy attribution must name the strategies individually"


def test_the_context_package_is_bounded() -> None:
    """"Smallest useful set" (FR-06.3), asserted on the request path.

    Committing many facts and confirming the package does not simply grow with them.
    """
    facts = [payload(f"Priya detail number {i}", "Priya") for i in range(12)]
    provider = FakeLLMProvider(
        completions=["noted"], structured_results=facts
    )
    with client_with(provider) as (client, container):
        for i in range(12):
            send(client, f"Priya detail number {i}")

        result = container.test_last_retrieval  # type: ignore[attr-defined]
        budget = container.retrieval.budget_for()
        assert len(result.facts) <= budget.max_items, (
            "retrieval must return the smallest useful set, not everything stored"
        )


def test_graph_outage_still_answers_from_postgres_with_disclosure() -> None:
    """NFR-06.5. PostgreSQL is the system of record, so a graph outage degrades
    retrieval rather than emptying it."""
    provider = FakeLLMProvider(
        completions=["noted"],
        structured_results=[
            payload("Priya lives in Pune", "Priya"),
            payload("asking again", "Priya"),
        ],
    )
    with client_with(provider) as (client, container):
        send(client, "Priya lives in Pune")

        # Break every graph strategy after the fact is committed.
        container.graph.fail_strategies = {  # type: ignore[attr-defined]
            "semantic",
            "fulltext",
            "entity",
            "temporal",
            "traversal",
        }

        send(client, "Where does Priya live?")

        result = container.test_last_retrieval  # type: ignore[attr-defined]
        assert result.diagnostics.degraded is True
        assert any("Pune" in f.statement for f in result.facts), (
            "PostgreSQL is the system of record; a graph outage must not empty memory"
        )
