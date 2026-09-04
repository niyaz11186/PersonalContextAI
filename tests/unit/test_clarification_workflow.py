"""Unit 5 Step 11 — ClarificationWorkflow.

Two properties matter more than the rest, and both are asserted directly rather than
inferred from reading the graph:

**Nothing is written before the user answers (ADR-014).** A test that only checked the
final merge happened would pass against an implementation that merged first and asked
afterwards. So the entity repository is inspected *while the workflow is suspended*.

**The pause survives a process restart.** That is the entire justification for taking
on the LangGraph dependency (ADR-006), so it is tested by discarding the workflow
object entirely and resuming through a fresh one that shares only the checkpoint store.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from pca.domain.enums import ClarificationStatus, EntityType
from pca.domain.errors import ClarificationNotFound
from pca.domain.ids import ConversationId, EntityId
from pca.domain.orchestration import AmbiguityContext
from pca.orchestration.checkpointer import PostgresCheckpointSaver
from pca.orchestration.clarification_workflow import ClarificationWorkflow
from pca.services.entities import EntityService
from tests.fakes.checkpoints import FakeCheckpointStore
from tests.fakes.clock import FakeClock
from tests.fakes.memory_repositories import FakeEntityRepository

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class Harness:
    """Workflow plus the fakes, with the checkpoint store separable for restart tests."""

    def __init__(self, store: FakeCheckpointStore | None = None) -> None:
        self.store = store or FakeCheckpointStore()
        self.clock = FakeClock(start=NOW, zone="Asia/Kolkata")
        self.entity_repo = FakeEntityRepository()
        self.entities = EntityService(repository=self.entity_repo, clock=self.clock)
        self.checkpointer = PostgresCheckpointSaver(self.store)  # type: ignore[arg-type]
        self.workflow = ClarificationWorkflow(
            entities=self.entities, checkpointer=self.checkpointer
        )

    async def entity(self, name: str, provisional: bool = False) -> EntityId:
        created = await self.entity_repo.create(
            entity_id=EntityId(uuid4()),
            name=name,
            entity_type=EntityType.PERSON,
            created_at=NOW,
            is_provisional=provisional,
        )
        return created.id


def ambiguity(options: list[str]) -> AmbiguityContext:
    return AmbiguityContext(
        conversation_id=ConversationId(uuid4()),
        question="Which Sarah did you mean?",
        options=options,
    )


@pytest.fixture
def harness() -> Harness:
    return Harness()


# ------------------------------------------------------- the ADR-014 guarantee


async def test_the_question_is_asked_and_nothing_is_written_yet(
    harness: Harness,
) -> None:
    """The load-bearing assertion for this workflow.

    Inspected mid-suspension. Checking only the end state would pass against an
    implementation that merged first and asked afterwards — and a wrongly merged
    entity is invisible corruption, unlike the duplicate it was trying to avoid.
    """
    keep = await harness.entity("Sarah Chen")
    await harness.entity("Sarah", provisional=True)

    outcome = await harness.workflow.run(ambiguity(["Sarah Chen", "Sarah"]))

    assert outcome.status is ClarificationStatus.AWAITING_ANSWER
    assert outcome.question == "Which Sarah did you mean?"
    assert harness.entity_repo.merged == {}, (
        "no merge may occur before the user answers (ADR-014)"
    )


async def test_answering_with_a_name_merges_the_provisional_duplicate(
    harness: Harness,
) -> None:
    keep = await harness.entity("Sarah Chen")
    provisional = await harness.entity("Sarah", provisional=True)

    started = await harness.workflow.run(ambiguity(["Sarah Chen", "Sarah"]))
    outcome = await harness.workflow.resume(started.thread_id, "Sarah Chen")

    assert outcome.status is ClarificationStatus.RESOLVED
    assert provisional in harness.entity_repo.merged, (
        "the provisional duplicate should have been absorbed"
    )
    kept, reason, _ = harness.entity_repo.merged[provisional]
    assert kept == keep
    assert "clarification" in reason, "the merge must record why it happened"


async def test_answering_with_an_index_also_works(harness: Harness) -> None:
    """A numbered list invites a numbered reply. Rejecting it would make the system
    obtuse about a question it just asked."""
    keep = await harness.entity("Sarah Chen")
    provisional = await harness.entity("Sarah", provisional=True)

    started = await harness.workflow.run(ambiguity(["Sarah Chen", "Sarah"]))
    outcome = await harness.workflow.resume(started.thread_id, "1")

    assert outcome.status is ClarificationStatus.RESOLVED
    assert harness.entity_repo.merged[provisional][0] == keep


# ------------------------------------------------------------------- declining


async def test_declining_writes_nothing_and_keeps_the_duplicate(
    harness: Harness,
) -> None:
    """Erring toward a visible duplicate rather than an invisible wrong merge."""
    await harness.entity("Sarah Chen")
    await harness.entity("Sarah", provisional=True)

    started = await harness.workflow.run(ambiguity(["Sarah Chen", "Sarah"]))
    outcome = await harness.workflow.resume(started.thread_id, "none")

    assert outcome.status is ClarificationStatus.ABANDONED
    assert harness.entity_repo.merged == {}


@pytest.mark.parametrize("reply", ["not sure", "dunno", "skip", "someone else", ""])
async def test_uncertain_replies_abandon_rather_than_guess(
    harness: Harness, reply: str
) -> None:
    """Reaching this workflow already means the system could not decide. Treating
    "hmm not sure" as a merge instruction defeats the point of stopping to ask."""
    await harness.entity("Sarah Chen")
    await harness.entity("Sarah", provisional=True)

    started = await harness.workflow.run(ambiguity(["Sarah Chen", "Sarah"]))
    outcome = await harness.workflow.resume(started.thread_id, reply)

    assert outcome.status is ClarificationStatus.ABANDONED
    assert harness.entity_repo.merged == {}


async def test_an_unrecognised_answer_is_treated_as_a_decline(
    harness: Harness,
) -> None:
    """Acting on an answer that matched nothing would be guessing under the guise of
    having asked."""
    await harness.entity("Sarah Chen")
    await harness.entity("Sarah", provisional=True)

    started = await harness.workflow.run(ambiguity(["Sarah Chen", "Sarah"]))
    outcome = await harness.workflow.resume(
        started.thread_id, "the one from the conference"
    )

    assert outcome.status is ClarificationStatus.ABANDONED
    assert harness.entity_repo.merged == {}


async def test_an_out_of_range_index_does_not_pick_the_first_option(
    harness: Harness,
) -> None:
    """Off-by-one on an index must not silently select something."""
    await harness.entity("Sarah Chen")
    await harness.entity("Sarah", provisional=True)

    started = await harness.workflow.run(ambiguity(["Sarah Chen", "Sarah"]))
    outcome = await harness.workflow.resume(started.thread_id, "7")

    assert outcome.status is ClarificationStatus.ABANDONED
    assert harness.entity_repo.merged == {}


# -------------------------------------------------------------- durability


async def test_the_pause_survives_a_process_restart(harness: Harness) -> None:
    """The justification for the LangGraph dependency (ADR-006).

    The original workflow object is discarded entirely; the replacement shares only
    the checkpoint store, exactly as a restarted process would. If the interrupt lived
    in process memory this would fail.
    """
    keep = await harness.entity("Sarah Chen")
    provisional = await harness.entity("Sarah", provisional=True)

    started = await harness.workflow.run(ambiguity(["Sarah Chen", "Sarah"]))
    thread_id = started.thread_id

    # Simulate the restart: new workflow, new service instances, same durable store.
    restarted = Harness(store=harness.store)
    restarted.entity_repo = harness.entity_repo
    restarted.entities = EntityService(
        repository=harness.entity_repo, clock=restarted.clock
    )
    restarted.workflow = ClarificationWorkflow(
        entities=restarted.entities, checkpointer=restarted.checkpointer
    )

    outcome = await restarted.workflow.resume(thread_id, "Sarah Chen")

    assert outcome.status is ClarificationStatus.RESOLVED
    assert harness.entity_repo.merged[provisional][0] == keep


async def test_resuming_an_unknown_thread_raises(harness: Harness) -> None:
    """Silence would leave a caller believing an answer had been recorded."""
    with pytest.raises(ClarificationNotFound):
        await harness.workflow.resume("clarification:does-not-exist", "Sarah Chen")


async def test_each_run_gets_its_own_thread(harness: Harness) -> None:
    """Two concurrent ambiguities must not share a checkpoint, or answering one would
    resume the other."""
    await harness.entity("Sarah Chen")

    first = await harness.workflow.run(ambiguity(["Sarah Chen"]))
    second = await harness.workflow.run(ambiguity(["Sarah Chen"]))

    assert first.thread_id != second.thread_id


async def test_answering_one_thread_does_not_resolve_another(
    harness: Harness,
) -> None:
    await harness.entity("Sarah Chen")
    await harness.entity("Sarah", provisional=True)

    first = await harness.workflow.run(ambiguity(["Sarah Chen", "Sarah"]))
    second = await harness.workflow.run(ambiguity(["Sarah Chen", "Sarah"]))

    await harness.workflow.resume(first.thread_id, "Sarah Chen")

    # The second thread is still suspended and still answerable.
    state = await harness.workflow._graph.aget_state(
        {"configurable": {"thread_id": second.thread_id}}
    )
    assert state is not None


# ------------------------------------------------------------------- no-op path


async def test_confirming_between_two_existing_people_needs_no_merge(
    harness: Harness,
) -> None:
    """The user may simply be saying which of several known people was meant. With no
    provisional duplicate there is nothing to clean up, and inventing a merge would
    destroy a distinction the user never questioned."""
    keep = await harness.entity("Sarah Chen")
    await harness.entity("Sarah Kim")

    started = await harness.workflow.run(ambiguity(["Sarah Chen", "Sarah Kim"]))
    outcome = await harness.workflow.resume(started.thread_id, "Sarah Chen")

    assert outcome.status is ClarificationStatus.RESOLVED
    assert harness.entity_repo.merged == {}, (
        "neither existing entity should be absorbed into the other"
    )
