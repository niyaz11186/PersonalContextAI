"""Unit 3 wired through the HTTP flow.

The unit tests prove the temporal logic. These prove it is actually CONNECTED — that
`ConflictDetectionService` is reached on the request path, that a temporal change
supersedes rather than contradicts, and that a contradiction reaches the user instead
of being silently resolved.

Worth having separately because a service can be fully correct, fully unit-tested, and
never invoked. Composition wires `conflicts` into the container; only a test through
the endpoint shows the call site exists.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

from fastapi.testclient import TestClient

from pca.services.conflicts import _Classification
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


def notices_from(response) -> list[str]:
    """Pull the notices out of the terminal SSE event."""
    for line in response.text.splitlines():
        if not line.startswith("data: "):
            continue
        event = json.loads(line[len("data: ") :])
        if event.get("done"):
            return list(event.get("notices", []))
    return []


@contextmanager
def client_with(provider: FakeLLMProvider):
    """Yields an entered TestClient plus the container holding the fakes.

    The client must be entered as a context manager: otherwise the lifespan never
    runs, `app.state.container` is never populated, and every request fails with an
    unrelated AttributeError.
    """
    container = build_fake_container(provider=provider)
    with make_client(container) as client:
        yield client, container


def send(client: TestClient, text: str):
    conversation_id = client.post("/conversations", json={}).json()["id"]
    return client.post(
        f"/conversations/{conversation_id}/messages", json={"content": text}
    )


def test_a_temporal_change_supersedes_and_keeps_both_states() -> None:
    """"She lives in Pune", then "she moved to Bangalore", through the API.

    The second message must NOT be treated as a contradiction. It ends the first
    fact's world validity and both remain stored — FR-04.4 through the real path.
    """
    provider = FakeLLMProvider(
        completions=["noted"],
        structured_results=[
            # message 1: extraction only, nothing to compare against yet
            payload("Priya lives in Pune", "Priya"),
            # message 2: extraction, then one classification against the stored fact
            payload("Priya lives in Bangalore", "Priya"),
            _Classification(
                kind="temporal_change",
                explanation="she moved",
                effective_from_phrase="",
            ),
        ],
    )
    with client_with(provider) as (client, container):
        send(client, "Priya lives in Pune")
        response = send(client, "Priya moved to Bangalore")

        assert response.status_code == 200

        facts = container.test_memory_repo.facts  # type: ignore[attr-defined]
        statements = {f.statement for f in facts.values()}
        assert "Priya lives in Pune" in statements, "the earlier state must be retained"
        assert "Priya lives in Bangalore" in statements

        bounded = [f for f in facts.values() if f.validity.valid_to is not None]
        assert bounded, "the superseded fact must gain a world-time end"
        assert all(f.belief.retracted_at is None for f in bounded), (
            "supersession must not retract belief — that would erase the earlier state"
        )

        # No contradiction notice: an ordinary life event must not demand arbitration.
        assert not any("conflicts with" in n for n in notices_from(response))


def test_a_contradiction_is_surfaced_to_the_user() -> None:
    """FR-05.6 through the API: both versions kept, the user told."""
    provider = FakeLLMProvider(
        completions=["noted"],
        structured_results=[
            payload("Priya works at Google", "Priya", category="identity"),
            payload("Priya works at Microsoft", "Priya", category="identity"),
            _Classification(
                kind="contradiction",
                explanation="she cannot hold both roles at once",
                effective_from_phrase="",
            ),
        ],
    )
    with client_with(provider) as (client, container):
        send(client, "Priya works at Google")
        response = send(client, "Priya works at Microsoft")

        notices = notices_from(response)
        assert any("conflicts with" in n for n in notices), (
            f"a contradiction must reach the user, got {notices}"
        )

        statements = {
            f.statement
            for f in container.test_memory_repo.facts.values()  # type: ignore[attr-defined]
        }
        assert "Priya works at Google" in statements
        assert "Priya works at Microsoft" in statements, (
            "neither version may be discarded; the system must not pick a winner"
        )


def test_commit_writes_belief_history_and_an_audit_entry() -> None:
    """Every committed fact is accountable: a belief window and a log entry."""
    provider = FakeLLMProvider(
        completions=["noted"],
        structured_results=[payload("Priya lives in Pune", "Priya")],
    )
    with client_with(provider) as (client, container):
        send(client, "Priya lives in Pune")

        assert container.test_belief_repo.transitions, (  # type: ignore[attr-defined]
            "a committed fact with no belief history is invisible to believed_at"
        )
        assert container.test_operation_repo.entries, (  # type: ignore[attr-defined]
            "specification §12 requires the mutation to be recorded"
        )


def test_the_commit_ran_in_a_single_transaction() -> None:
    """One episode, one transaction. Unit 2 used several and left half-written state."""
    provider = FakeLLMProvider(
        completions=["noted"],
        structured_results=[payload("Priya lives in Pune", "Priya")],
    )
    with client_with(provider) as (client, container):
        send(client, "Priya lives in Pune")

        transactions = container.test_transactions  # type: ignore[attr-defined]
        assert transactions.committed == 1
        assert transactions.rolled_back == 0
