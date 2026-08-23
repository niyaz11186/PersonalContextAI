"""Regression tests for the GraphitiMemoryAdapter call contract.

These exist because of a specific defect that disabled the entire memory pipeline
while every symptom looked benign.

The adapter passed our PostgreSQL episode id as Graphiti's `uuid` parameter,
intending it to assign an id to a new episode. Graphiti's `uuid` means the
opposite: "update the existing episode with this id". It calls
`EpisodicNode.get_by_uuid` and raises "node not found" when absent.

Consequence: every ingestion failed, the graph stayed empty, retrieval returned
zero hits, and the assistant correctly reported having no history. Nothing errored
at the API level. The bug was only found by reading logs after a manual
cross-conversation test failed.

It went undetected because the adapter constructed its own Graphiti client and
could not be exercised without a live Neo4j. The client is now injectable, and
these tests assert the call contract directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from pca.adapters.graphiti.memory_graph import GraphitiMemoryAdapter
from pca.domain.conversation import Episode
from pca.domain.errors import MemoryGraphUnavailable
from pca.domain.ids import ConversationId, EpisodeId, MessageId

ANCHOR = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
GRAPH_UUID = "graphiti-assigned-uuid-123"


class _StubEpisodeNode:
    uuid = GRAPH_UUID


class _StubResults:
    episode = _StubEpisodeNode()
    nodes = ["entity-a", "entity-b"]
    edges = ["edge-a"]


class StubGraphiti:
    """Records how add_episode was called."""

    def __init__(self, raise_on_call: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raise = raise_on_call

    async def add_episode(self, **kwargs: Any) -> _StubResults:
        self.calls.append(kwargs)
        if self._raise:
            raise self._raise
        return _StubResults()

    async def search(self, **kwargs: Any) -> list[Any]:
        return []


def make_adapter(stub: StubGraphiti) -> GraphitiMemoryAdapter:
    return GraphitiMemoryAdapter(
        uri="bolt://unused",
        user="neo4j",
        password="unused",
        api_key="unused",
        llm_model="m",
        small_model="s",
        embedding_model="e",
        reranker_model="r",
        graphiti=stub,
    )


def make_episode(message: bool = True) -> Episode:
    return Episode(
        id=EpisodeId(uuid4()),
        content="My sister Priya lives in Pune.",
        occurred_at=ANCHOR,
        zone="Asia/Kolkata",
        conversation_id=ConversationId(uuid4()),
        message_id=MessageId(uuid4()) if message else None,
    )


async def test_uuid_is_never_passed_to_add_episode() -> None:
    """The actual regression.

    Passing `uuid` makes Graphiti attempt to load a non-existent node and fail.
    This single assertion is the difference between a working memory system and one
    that silently stores nothing.
    """
    stub = StubGraphiti()

    await make_adapter(stub).add_episode(make_episode())

    assert "uuid" not in stub.calls[0], (
        "uuid must not be passed: Graphiti treats it as 'update existing episode' "
        "and raises 'node not found'"
    )


async def test_reference_time_is_the_episode_time_not_now() -> None:
    """Using ingestion time would collapse all history onto today.

    Critical for imported documents and for background extraction, where ingestion
    can happen long after the event.
    """
    stub = StubGraphiti()

    episode = make_episode()
    await make_adapter(stub).add_episode(episode)

    assert stub.calls[0]["reference_time"] == episode.occurred_at
    assert stub.calls[0]["reference_time"] == ANCHOR


async def test_graph_assigned_uuid_is_returned_as_the_reference() -> None:
    """We store Graphiti's id, not ours, since Graphiti owns node identity."""
    stub = StubGraphiti()

    result = await make_adapter(stub).add_episode(make_episode())

    assert result.episode_ref == GRAPH_UUID
    assert result.entities_touched == 2
    assert result.edges_touched == 1


async def test_episode_body_and_source_are_mapped() -> None:
    stub = StubGraphiti()
    episode = make_episode()

    await make_adapter(stub).add_episode(episode)

    call = stub.calls[0]
    assert call["episode_body"] == episode.content
    assert "conversation message" in call["source_description"]


async def test_imported_document_uses_the_text_source_type() -> None:
    """A document is not a chat message; Graphiti's extraction differs by source."""
    from graphiti_core.nodes import EpisodeType

    stub = StubGraphiti()
    episode = Episode(
        id=EpisodeId(uuid4()),
        content="journal entry",
        occurred_at=ANCHOR,
        zone="UTC",
        document_id=uuid4(),
    )

    await make_adapter(stub).add_episode(episode)

    assert stub.calls[0]["source"] is EpisodeType.text
    assert "imported document" in stub.calls[0]["source_description"]


async def test_ingestion_failure_is_translated_to_a_domain_error() -> None:
    """Graphiti exceptions must not leak past the adapter (boundary rule 1)."""
    stub = StubGraphiti(raise_on_call=RuntimeError("node xyz not found"))

    with pytest.raises(MemoryGraphUnavailable, match="episode ingestion failed"):
        await make_adapter(stub).add_episode(make_episode())


async def test_zero_extraction_is_not_an_error() -> None:
    """A message with no durable content should ingest cleanly with zero entities.

    Distinguishing "nothing worth extracting" from "extraction is broken" matters:
    the first is normal, the second is the failure this whole file exists to catch.
    """

    class EmptyResults:
        episode = _StubEpisodeNode()
        nodes: list[str] = []
        edges: list[str] = []

    class EmptyGraphiti(StubGraphiti):
        async def add_episode(self, **kwargs: Any) -> EmptyResults:  # type: ignore[override]
            self.calls.append(kwargs)
            return EmptyResults()

    stub = EmptyGraphiti()
    result = await make_adapter(stub).add_episode(make_episode())

    assert result.entities_touched == 0
    assert result.episode_ref == GRAPH_UUID
