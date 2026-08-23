"""Tests for ConversationService.

The emphasis is on the guarantees the product actually depends on:

  - source material is append-only (FR-01.4)
  - ordering is by sequence, not timestamp
  - the capture anchor and zone are recorded at utterance time, because
    extraction runs later in the background and resolving "last Tuesday" against
    the extraction time would shift dates (ADR-008 + ADR-010)
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pca.domain.enums import Role
from pca.services.conversation import ConversationService
from tests.fakes.clock import FakeClock
from tests.fakes.repositories import FakeConversationRepository

START = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=START, zone="Asia/Kolkata")


@pytest.fixture
def repository() -> FakeConversationRepository:
    return FakeConversationRepository()


@pytest.fixture
def service(
    repository: FakeConversationRepository, clock: FakeClock
) -> ConversationService:
    return ConversationService(repository=repository, clock=clock)


async def test_create_conversation_records_clock_time_and_zone(
    service: ConversationService, clock: FakeClock
) -> None:
    conversation = await service.create_conversation(title="Family")

    assert conversation.started_at == clock.now()
    assert conversation.zone == "Asia/Kolkata"
    assert conversation.title == "Family"


async def test_append_message_captures_anchor_and_zone(
    service: ConversationService, clock: FakeClock
) -> None:
    """The anchor must be utterance time, not extraction time.

    Extraction runs in the background (ADR-008), potentially minutes later.
    Anchoring "last Tuesday" to extraction time instead of when it was said would
    silently shift the resolved date.
    """
    conversation = await service.create_conversation()
    clock.advance(minutes=5)

    message = await service.append_message(conversation.id, Role.USER, "I saw Priya last Tuesday")

    assert message.captured_at == START.replace(hour=9, minute=5)
    assert message.zone == "Asia/Kolkata"


async def test_history_is_chronological_not_insertion_ordered(
    service: ConversationService, clock: FakeClock
) -> None:
    conversation = await service.create_conversation()
    for text in ["first", "second", "third"]:
        await service.append_message(conversation.id, Role.USER, text)
        clock.advance(seconds=30)

    history = await service.get_history(conversation.id)

    assert [m.content for m in history] == ["first", "second", "third"]


async def test_history_ordering_survives_identical_timestamps(
    service: ConversationService,
) -> None:
    """Ordering must not depend on timestamps being distinct.

    The clock is deliberately not advanced here. Two messages sharing an instant
    is entirely possible, and ordering by timestamp would be non-deterministic —
    which is why both the real adapter and the fake order by a sequence column.
    """
    conversation = await service.create_conversation()
    for text in ["a", "b", "c", "d"]:
        await service.append_message(conversation.id, Role.USER, text)

    history = await service.get_history(conversation.id)

    assert [m.content for m in history] == ["a", "b", "c", "d"]
    assert len({m.captured_at for m in history}) == 1  # all identical instants


async def test_history_limit_returns_the_most_recent_messages(
    service: ConversationService,
) -> None:
    """A limit must keep the newest, not the oldest.

    Returning the first n would give the model the least relevant part of the
    conversation.
    """
    conversation = await service.create_conversation()
    for index in range(10):
        await service.append_message(conversation.id, Role.USER, f"msg-{index}")

    history = await service.get_history(conversation.id, limit=3)

    assert [m.content for m in history] == ["msg-7", "msg-8", "msg-9"]


async def test_history_is_isolated_per_conversation(service: ConversationService) -> None:
    first = await service.create_conversation(title="one")
    second = await service.create_conversation(title="two")
    await service.append_message(first.id, Role.USER, "in first")
    await service.append_message(second.id, Role.USER, "in second")

    assert [m.content for m in await service.get_history(first.id)] == ["in first"]
    assert [m.content for m in await service.get_history(second.id)] == ["in second"]


async def test_service_exposes_no_mutation_of_messages(
    service: ConversationService,
) -> None:
    """FR-01.4 is enforced by omission.

    Immutability of source material is a structural property here: there is no
    update or delete method to call. This test exists so that adding one is a
    deliberate, visible act rather than an accident.
    """
    forbidden = {"update_message", "delete_message", "edit_message", "replace_message"}
    assert forbidden.isdisjoint(dir(service))


async def test_messages_are_frozen(service: ConversationService) -> None:
    conversation = await service.create_conversation()
    message = await service.append_message(conversation.id, Role.USER, "immutable")

    with pytest.raises((AttributeError, TypeError)):
        message.content = "changed"  # type: ignore[misc]


async def test_assistant_and_user_roles_both_persist(service: ConversationService) -> None:
    conversation = await service.create_conversation()
    await service.append_message(conversation.id, Role.USER, "question")
    await service.append_message(conversation.id, Role.ASSISTANT, "answer")

    history = await service.get_history(conversation.id)

    assert [m.role for m in history] == [Role.USER, Role.ASSISTANT]


async def test_get_surrounding_returns_window_around_target(
    service: ConversationService,
) -> None:
    """Provenance excerpts need context, not an isolated sentence (FR-09.3)."""
    conversation = await service.create_conversation()
    messages = []
    for index in range(7):
        messages.append(await service.append_message(conversation.id, Role.USER, f"m{index}"))

    excerpt = await service.get_surrounding(messages[3].id, window=2)

    assert [m.content for m in excerpt] == ["m1", "m2", "m3", "m4", "m5"]


async def test_list_conversations_is_newest_first(
    service: ConversationService, clock: FakeClock
) -> None:
    await service.create_conversation(title="oldest")
    clock.advance(hours=1)
    await service.create_conversation(title="middle")
    clock.advance(hours=1)
    await service.create_conversation(title="newest")

    listed = await service.list_conversations()

    assert [c.title for c in listed] == ["newest", "middle", "oldest"]


async def test_zone_change_is_recorded_per_message(
    service: ConversationService, clock: FakeClock
) -> None:
    """ADR-011 stores the zone per record, not globally.

    If the user relocates, previously captured messages must keep the zone that
    was active when they were said — otherwise a global setting retroactively
    reinterprets every past day boundary.
    """
    conversation = await service.create_conversation()
    first = await service.append_message(conversation.id, Role.USER, "said in India")

    clock.set_zone("Europe/London")
    second = await service.append_message(conversation.id, Role.USER, "said in London")

    assert first.zone == "Asia/Kolkata"
    assert second.zone == "Europe/London"
