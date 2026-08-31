"""PostgresCheckpointSaver — the machinery Unit 5's completion criterion rests on.

> Clarification workflow interrupts, survives a process restart, and resumes with
> intact state.

The restart half is what makes these tests non-trivial. Resuming a graph inside the
same process proves almost nothing: LangGraph holds the state in memory and would
resume even with a checkpointer that discarded everything. So every restart test here
throws away the compiled graph AND the saver, keeping only the store, which is the
part that would survive a real process death.
"""

from __future__ import annotations

from typing import TypedDict

import pytest
from langgraph.checkpoint.base import WRITES_IDX_MAP, empty_checkpoint
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from pca.orchestration.checkpointer import PostgresCheckpointSaver
from pca.ports.checkpoints import CheckpointRow, CheckpointWriteRow
from tests.fakes.checkpoints import FakeCheckpointStore

pytestmark = pytest.mark.asyncio


class _State(TypedDict, total=False):
    subject: str
    answer: str
    resolved: str


def _build(saver: PostgresCheckpointSaver):
    """A graph that stops and waits, the way ClarificationWorkflow will."""

    def ask(state: _State) -> _State:
        answer = interrupt({"question": f"which {state['subject']}?"})
        return {"answer": answer}

    def apply(state: _State) -> _State:
        return {"resolved": f"{state['subject']}={state['answer']}"}

    builder = StateGraph(_State)
    builder.add_node("ask", ask)
    builder.add_node("apply", apply)
    builder.add_edge(START, "ask")
    builder.add_edge("ask", "apply")
    builder.add_edge("apply", END)
    return builder.compile(checkpointer=saver)


# --------------------------------------------------------------------- store I/O


async def test_a_checkpoint_round_trips_through_the_store() -> None:
    store = FakeCheckpointStore()
    saver = PostgresCheckpointSaver(store, workflow="test")
    config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}

    checkpoint = empty_checkpoint()
    returned = await saver.aput(config, checkpoint, {"source": "input"}, {})

    assert returned["configurable"]["checkpoint_id"] == checkpoint["id"]

    loaded = await saver.aget_tuple(
        {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
    )
    assert loaded is not None
    assert loaded.checkpoint["id"] == checkpoint["id"]
    assert loaded.metadata["source"] == "input"


async def test_namespaces_do_not_collide() -> None:
    """The defect migration 0004 exists to fix.

    The 0001 table keyed on (thread_id, checkpoint_id) with no namespace, so a
    subgraph writing a checkpoint would overwrite its parent's. Pinned here because
    the symptom — a parent graph resuming into a child's state — is not obviously a
    storage bug when you meet it.
    """
    store = FakeCheckpointStore()
    saver = PostgresCheckpointSaver(store)

    parent = empty_checkpoint()
    child = empty_checkpoint()

    await saver.aput(
        {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}},
        parent,
        {"source": "loop"},
        {},
    )
    await saver.aput(
        {"configurable": {"thread_id": "t1", "checkpoint_ns": "child"}},
        child,
        {"source": "loop"},
        {},
    )

    from_parent = await saver.aget_tuple(
        {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
    )
    from_child = await saver.aget_tuple(
        {"configurable": {"thread_id": "t1", "checkpoint_ns": "child"}}
    )

    assert from_parent is not None and from_child is not None
    assert from_parent.checkpoint["id"] == parent["id"]
    assert from_child.checkpoint["id"] == child["id"]


async def test_ordinary_writes_are_not_overwritten_by_a_replay() -> None:
    """Insert-if-absent for idx >= 0, per the InMemorySaver reference.

    A resumed task re-emits its writes. Overwriting would replace the value captured
    at interrupt time with one produced during replay — silently discarding the state
    the checkpoint existed to preserve.
    """
    store = FakeCheckpointStore()
    saver = PostgresCheckpointSaver(store)
    config = {
        "configurable": {
            "thread_id": "t1",
            "checkpoint_ns": "",
            "checkpoint_id": "c1",
        }
    }

    await saver.aput_writes(config, [("channel", "original")], task_id="task-1")
    await saver.aput_writes(config, [("channel", "replayed")], task_id="task-1")

    writes = await store.get_writes("t1", "", "c1")
    assert len(writes) == 1
    assert saver.serde.loads_typed((writes[0].type, writes[0].payload)) == "original"


async def test_special_writes_do_overwrite_and_use_fixed_indices() -> None:
    """The counterpart rule: interrupt/resume payloads take the latest value.

    Special channels also take fixed NEGATIVE indices from WRITES_IDX_MAP rather than
    their position in the sequence. Positional indices would let an ordinary write
    collide with an interrupt payload.
    """
    store = FakeCheckpointStore()
    saver = PostgresCheckpointSaver(store)
    config = {
        "configurable": {
            "thread_id": "t1",
            "checkpoint_ns": "",
            "checkpoint_id": "c1",
        }
    }

    await saver.aput_writes(config, [("__interrupt__", "first")], task_id="task-1")
    await saver.aput_writes(config, [("__interrupt__", "second")], task_id="task-1")

    writes = await store.get_writes("t1", "", "c1")
    assert len(writes) == 1
    assert writes[0].idx == WRITES_IDX_MAP["__interrupt__"] < 0
    assert saver.serde.loads_typed((writes[0].type, writes[0].payload)) == "second"


async def test_deleting_a_thread_removes_its_pending_writes_too() -> None:
    store = FakeCheckpointStore()
    saver = PostgresCheckpointSaver(store)

    await store.put(
        CheckpointRow(
            thread_id="t1", checkpoint_ns="", checkpoint_id="c1", type="x", payload=b"y"
        )
    )
    await store.put_writes(
        [
            CheckpointWriteRow(
                thread_id="t1",
                checkpoint_ns="",
                checkpoint_id="c1",
                task_id="task-1",
                idx=0,
                channel="c",
                type="x",
                payload=b"y",
            )
        ]
    )

    await saver.adelete_thread("t1")

    assert not store.rows
    # Orphaned writes would be silently inherited by a later thread reusing the id.
    assert not store.writes


async def test_the_sync_surface_refuses_rather_than_silently_doing_nothing() -> None:
    saver = PostgresCheckpointSaver(FakeCheckpointStore())
    with pytest.raises(NotImplementedError, match="aget_tuple"):
        saver.get_tuple({"configurable": {"thread_id": "t1"}})


# ------------------------------------------------------- interrupt and resume


async def test_a_graph_interrupts_and_persists_its_state() -> None:
    store = FakeCheckpointStore()
    graph = _build(PostgresCheckpointSaver(store, workflow="clarification"))
    config = {"configurable": {"thread_id": "conv-1"}}

    result = await graph.ainvoke({"subject": "Priya"}, config)

    assert "__interrupt__" in result
    assert result["__interrupt__"][0].value == {"question": "which Priya?"}
    assert "resolved" not in result
    assert store.rows, "an interrupt that persisted nothing cannot be resumed"


async def test_it_resumes_with_intact_state_after_a_simulated_restart() -> None:
    """The Unit 5 completion criterion, at unit level.

    The graph and the saver are both discarded between interrupt and resume, and only
    the store survives. That is the difference between testing persistence and
    testing that LangGraph remembers things within one process.
    """
    store = FakeCheckpointStore()
    config = {"configurable": {"thread_id": "conv-1"}}

    first = _build(PostgresCheckpointSaver(store))
    await first.ainvoke({"subject": "Priya"}, config)
    del first

    # New process: new saver, new compiled graph, same durable store.
    second = _build(PostgresCheckpointSaver(store))
    resumed = await second.ainvoke(Command(resume="the one from work"), config)

    assert resumed["resolved"] == "Priya=the one from work"
    # `subject` was set before the interrupt and never re-supplied. Its presence is
    # what proves the state was restored rather than reconstructed from the resume.
    assert resumed["subject"] == "Priya"


async def test_history_is_readable_after_a_restart() -> None:
    store = FakeCheckpointStore()
    config = {"configurable": {"thread_id": "conv-1"}}

    await _build(PostgresCheckpointSaver(store)).ainvoke({"subject": "Priya"}, config)

    saver = PostgresCheckpointSaver(store)
    seen = [tuple_ async for tuple_ in saver.alist(config)]

    assert seen, "alist returned nothing for a thread that has checkpoints"
    # Newest first, so a caller taking the head gets the current state.
    assert seen == sorted(
        seen, key=lambda t: t.config["configurable"]["checkpoint_id"], reverse=True
    )
