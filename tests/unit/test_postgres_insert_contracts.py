"""Insert-statement contract tests for the PostgreSQL memory repositories.

These exist because of a specific class of defect found during the Unit 2 audit.

Three `NOT NULL` columns were populated by deriving a value from a domain field that
is frequently `None`:

    facts.created_at         <- fact.belief.asserted_at        (always set, fine)
    events.created_at        <- event.occurred_at or ...       (None without a date)
    relationships.created_at <- relationship.validity.valid_from (None by default)

The last two would have raised a NOT NULL violation on the *first* insert. Worse, the
relationship case would have broken exactly the capability Unit 2 was built to add —
the "friend" relationship that Unit 1b failed to capture.

No database is available here, so the assertions inspect the compiled statement
parameters instead. That is enough to catch a NULL bound to a NOT NULL column, which
is the failure that actually occurred.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Insert

from pca.adapters.postgres.memory_repositories import PostgresMemoryRepository
from pca.adapters.postgres.tables import events, facts, relationships
from pca.domain.enums import Confidence, Origin
from pca.domain.ids import EntityId, EpisodeId, MemoryId
from pca.domain.memory import Event, Fact, ProvenanceRef, Relationship
from pca.domain.temporal import BeliefWindow, TemporalValidity
from tests.fakes.clock import FakeClock
from tests.fakes.store import FakeRelationalStore

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def store() -> FakeRelationalStore:
    return FakeRelationalStore()


@pytest.fixture
def repository(store: FakeRelationalStore) -> PostgresMemoryRepository:
    return PostgresMemoryRepository(store, FakeClock(start=NOW))  # type: ignore[arg-type]


def params_for(store: FakeRelationalStore, table_name: str) -> dict[str, Any]:
    """Bound parameters of the insert targeting `table_name`."""
    for statement, _ in store.statements:
        if isinstance(statement, Insert) and statement.table.name == table_name:
            return dict(statement.compile().params)
    raise AssertionError(f"no INSERT recorded for {table_name}")


def not_null_columns(table) -> set[str]:  # type: ignore[no-untyped-def]
    """Columns the schema requires, excluding those with a server default."""
    return {
        column.name
        for column in table.columns
        if not column.nullable and column.server_default is None
    }


def a_fact() -> Fact:
    return Fact(
        id=MemoryId(uuid4()),
        statement="Suresh is a frontend developer",
        origin=Origin.USER_STATED,
        confidence=Confidence.PROBABLE,
        validity=TemporalValidity(),
        belief=BeliefWindow(asserted_at=NOW),
        provenance=[ProvenanceRef(episode_id=EpisodeId(uuid4()))],
        salience=0.75,
    )


def an_event() -> Event:
    """Deliberately carries no temporal information.

    This is the shape that broke: an event with no resolved date derived a NULL
    created_at.
    """
    return Event(
        id=MemoryId(uuid4()),
        description="They argued",
        origin=Origin.USER_STATED,
        provenance=[ProvenanceRef(episode_id=EpisodeId(uuid4()))],
    )


def a_relationship() -> Relationship:
    """Default validity, which is how every relationship arrives from extraction."""
    return Relationship(
        id=MemoryId(uuid4()),
        from_entity_id=EntityId(uuid4()),
        to_entity_id=EntityId(uuid4()),
        relation_type="friend",
        origin=Origin.USER_STATED,
        provenance=[ProvenanceRef(episode_id=EpisodeId(uuid4()))],
    )


# --------------------------------------------------------------------- facts


async def test_fact_insert_supplies_every_required_column(
    repository: PostgresMemoryRepository, store: FakeRelationalStore
) -> None:
    await repository.insert_fact(a_fact(), salience_category="identity")

    bound = params_for(store, "facts")
    for column in not_null_columns(facts):
        assert bound.get(column) is not None, f"facts.{column} bound to NULL"


# --------------------------------------------------------------------- events


async def test_event_without_a_date_still_supplies_created_at(
    repository: PostgresMemoryRepository, store: FakeRelationalStore
) -> None:
    """The first of the two audit bugs.

    `created_at` was derived from the event's own date, so an undated event bound
    NULL to a NOT NULL column.
    """
    await repository.insert_event(an_event(), salience_category="significant_event")

    bound = params_for(store, "events")
    assert bound.get("created_at") is not None
    assert bound["created_at"] == NOW


async def test_event_insert_supplies_every_required_column(
    repository: PostgresMemoryRepository, store: FakeRelationalStore
) -> None:
    await repository.insert_event(an_event(), salience_category=None)

    bound = params_for(store, "events")
    for column in not_null_columns(events):
        assert bound.get(column) is not None, f"events.{column} bound to NULL"


async def test_undated_event_leaves_temporal_columns_null(
    repository: PostgresMemoryRepository, store: FakeRelationalStore
) -> None:
    """ADR-010: absent time must stay absent, never be filled with the write time.

    `created_at` and `occurred_at` are different facts about the world and must not
    collapse into each other.
    """
    await repository.insert_event(an_event(), salience_category=None)

    bound = params_for(store, "events")
    assert bound.get("occurred_at") is None
    assert bound.get("temporal_raw_phrase") is None


# -------------------------------------------------------------- relationships


async def test_relationship_with_default_validity_still_supplies_created_at(
    repository: PostgresMemoryRepository, store: FakeRelationalStore
) -> None:
    """The second audit bug, and the more damaging one.

    `created_at` was derived from `validity.valid_from`, which is None for every
    relationship extraction produces. Every "friend" relationship — the exact gap
    Unit 2 exists to close — would have failed to insert.
    """
    await repository.insert_relationship(a_relationship())

    bound = params_for(store, "relationships")
    assert bound.get("created_at") is not None
    assert bound["created_at"] == NOW


async def test_relationship_insert_supplies_every_required_column(
    repository: PostgresMemoryRepository, store: FakeRelationalStore
) -> None:
    await repository.insert_relationship(a_relationship())

    bound = params_for(store, "relationships")
    for column in not_null_columns(relationships):
        assert bound.get(column) is not None, f"relationships.{column} bound to NULL"


async def test_relationship_endpoints_are_distinct_by_construction() -> None:
    """The schema forbids self-links, so the domain type rejects them first.

    Failing at construction gives a clear error instead of a constraint violation
    surfacing from inside a transaction.
    """
    same = EntityId(uuid4())
    with pytest.raises(ValueError, match="two distinct entities"):
        Relationship(
            id=MemoryId(uuid4()),
            from_entity_id=same,
            to_entity_id=same,
            relation_type="knows",
            origin=Origin.USER_STATED,
            provenance=[ProvenanceRef(episode_id=EpisodeId(uuid4()))],
        )


# ------------------------------------------------------------------ subjects


async def test_fact_subject_rows_are_written_for_each_entity(
    repository: PostgresMemoryRepository, store: FakeRelationalStore
) -> None:
    fact = a_fact()
    subjects = [EntityId(uuid4()), EntityId(uuid4())]
    linked = Fact(
        id=fact.id,
        statement=fact.statement,
        origin=fact.origin,
        confidence=fact.confidence,
        validity=fact.validity,
        belief=fact.belief,
        provenance=fact.provenance,
        salience=fact.salience,
        subject_entity_ids=subjects,
    )

    await repository.insert_fact(linked, salience_category=None)

    inserts = [
        s
        for s, _ in store.statements
        if isinstance(s, Insert) and s.table.name == "fact_subjects"
    ]
    assert len(inserts) == 2
