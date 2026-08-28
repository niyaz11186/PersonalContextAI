"""PostgreSQL implementations of the Unit 2 repository ports.

Layer L5. SQLAlchemy Core only — never the ORM, and never in L3 (boundary rule 3).

Three repositories live here because they share the same row-mapping helpers and are
always used together during a commit.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, func, insert, or_, select, update

from pca.adapters.postgres.tables import (
    entities as entities_table,
)
from pca.adapters.postgres.tables import (
    entity_aliases,
    event_participants,
    events,
    fact_subjects,
    facts,
    provenance_index,
    relationships,
)
from pca.domain.enums import (
    Confidence,
    EntityType,
    Granularity,
    MemoryKind,
    Origin,
    ResolutionMethod,
)
from pca.domain.ids import EntityId, EpisodeId, MemoryId
from pca.domain.memory import (
    Entity,
    Event,
    Fact,
    ProvenanceRef,
    Relationship,
)
from pca.domain.temporal import BeliefWindow, TemporalExpression, TemporalValidity
from pca.adapters.postgres.scope import scope
from pca.observability.logging import get_logger
from pca.ports.clock import ClockPort
from pca.ports.store import RelationalStorePort, Transaction

_log = get_logger(__name__)


def _temporal_expression(row: Any) -> TemporalExpression | None:
    """Rebuild the stored time phrase.

    The descriptor is deliberately not persisted — only the raw phrase and the
    resolution outcome are. Re-resolution from the raw phrase against the original
    anchor is always possible, and storing a redundant parsed form invites the two
    drifting apart.
    """
    phrase = row["temporal_raw_phrase"]
    if not phrase:
        return None
    return TemporalExpression(
        raw_phrase=phrase,
        granularity=Granularity(row["temporal_granularity"] or "unknown"),
        method=ResolutionMethod(row["temporal_method"] or "unresolved"),
        anchor_zone=row["temporal_anchor_zone"] or "UTC",
        resolved_from=row.get("valid_from") or row.get("occurred_at"),
        resolved_to=row.get("valid_to") or row.get("occurred_through"),
    )


class PostgresEntityRepository:
    def __init__(self, store: RelationalStorePort) -> None:
        self._store = store

    async def create(
        self,
        entity_id: EntityId,
        name: str,
        entity_type: EntityType,
        created_at: datetime,
        is_provisional: bool = False,
        aliases: Sequence[str] = (),
        tx: Transaction | None = None,
    ) -> Entity:
        async with scope(self._store, tx) as t:
            await t.execute(
                insert(entities_table).values(
                    id=entity_id,
                    name=name,
                    entity_type=entity_type.value,
                    is_provisional=is_provisional,
                    created_at=created_at,
                )
            )
            if aliases:
                # Same scope: an entity created with aliases must not be able to land
                # without them, or self-entity resolution would stop converging.
                await self.add_aliases(entity_id, aliases, tx=t)
        return Entity(
            id=entity_id,
            name=name,
            entity_type=entity_type,
            aliases=list(aliases),
            is_provisional=is_provisional,
        )

    async def get(self, entity_id: EntityId) -> Entity | None:
        row = await self._store.fetch_one(
            select(entities_table).where(
                and_(
                    entities_table.c.id == entity_id,
                    entities_table.c.deleted_at.is_(None),
                    entities_table.c.merged_into.is_(None),
                )
            )
        )
        if row is None:
            return None
        return await self._hydrate(row)

    async def find_by_name(self, name: str) -> Sequence[Entity]:
        """Every live entity matching by name or alias, case-insensitively.

        Returns all matches deliberately. Collapsing to a single "best" result here
        would hide the ambiguity ADR-014 exists to surface, and the caller would have
        no way to know it was choosing blindly.
        """
        lowered = name.strip().casefold()
        alias_match = (
            select(entity_aliases.c.entity_id)
            .where(func.lower(entity_aliases.c.alias) == lowered)
            .scalar_subquery()
        )
        rows = await self._store.fetch_all(
            select(entities_table).where(
                and_(
                    entities_table.c.deleted_at.is_(None),
                    entities_table.c.merged_into.is_(None),
                    or_(
                        func.lower(entities_table.c.name) == lowered,
                        entities_table.c.id.in_(alias_match),
                    ),
                )
            )
        )
        return [await self._hydrate(row) for row in rows]

    async def list_provisional(self, limit: int = 100) -> Sequence[Entity]:
        rows = await self._store.fetch_all(
            select(entities_table)
            .where(
                and_(
                    entities_table.c.is_provisional.is_(True),
                    entities_table.c.deleted_at.is_(None),
                    entities_table.c.merged_into.is_(None),
                )
            )
            .order_by(entities_table.c.created_at.desc())
            .limit(limit)
        )
        return [await self._hydrate(row) for row in rows]

    async def add_aliases(
        self,
        entity_id: EntityId,
        aliases: Sequence[str],
        tx: Transaction | None = None,
    ) -> None:
        async with scope(self._store, tx) as t:
            for alias in aliases:
                cleaned = alias.strip()
                if not cleaned:
                    continue
                # Idempotent: the primary key is (entity_id, alias), so a repeated
                # merge must not fail on a duplicate. The check reads through the same
                # scope as the insert, so it sees aliases added earlier in this loop.
                existing = await t.fetch_one(
                    select(entity_aliases).where(
                        and_(
                            entity_aliases.c.entity_id == entity_id,
                            entity_aliases.c.alias == cleaned,
                        )
                    )
                )
                if existing is None:
                    await t.execute(
                        insert(entity_aliases).values(
                            entity_id=entity_id, alias=cleaned
                        )
                    )

    async def merge(
        self,
        keep: EntityId,
        absorb: EntityId,
        reason: str,
        merged_at: datetime,
        tx: Transaction | None = None,
    ) -> None:
        """Point the absorbed entity at the survivor and repoint its references.

        The absorbed row is retained rather than deleted so the operation stays
        reversible (ADR-014). Its fact, event, and relationship links are moved so
        queries about the surviving entity see the full picture.

        Accepts `tx` so the merge and its operation-log entry commit together. A merge
        that landed without its log entry would be an irreversible merge, which is
        precisely what ADR-014 forbids.
        """
        async with scope(self._store, tx) as tx:
            await tx.execute(
                update(entities_table)
                .where(entities_table.c.id == absorb)
                .values(merged_into=keep, merged_at=merged_at, merge_reason=reason)
            )

            # Repoint join rows. Delete-then-insert rather than update, because the
            # target pair may already exist and the primary key would collide.
            for table, id_column in (
                (fact_subjects, fact_subjects.c.fact_id),
                (event_participants, event_participants.c.event_id),
            ):
                rows = await tx.fetch_all(
                    select(id_column).where(table.c.entity_id == absorb)
                )
                await tx.execute(delete(table).where(table.c.entity_id == absorb))
                for row in rows:
                    owner = next(iter(row.values()))
                    already = await tx.fetch_one(
                        select(table).where(
                            and_(id_column == owner, table.c.entity_id == keep)
                        )
                    )
                    if already is None:
                        await tx.execute(
                            insert(table).values(
                                **{id_column.name: owner, "entity_id": keep}
                            )
                        )

            await tx.execute(
                update(relationships)
                .where(relationships.c.from_entity_id == absorb)
                .values(from_entity_id=keep)
            )
            await tx.execute(
                update(relationships)
                .where(relationships.c.to_entity_id == absorb)
                .values(to_entity_id=keep)
            )
            # Repointing can produce a self-referential relationship, which the
            # schema forbids and which carries no meaning.
            await tx.execute(
                delete(relationships).where(
                    relationships.c.from_entity_id == relationships.c.to_entity_id
                )
            )

    async def count(self) -> int:
        row = await self._store.fetch_one(
            select(func.count())
            .select_from(entities_table)
            .where(
                and_(
                    entities_table.c.deleted_at.is_(None),
                    entities_table.c.merged_into.is_(None),
                )
            )
        )
        return int(next(iter(row.values()))) if row else 0

    async def _hydrate(self, row: Any) -> Entity:
        aliases = await self._store.fetch_all(
            select(entity_aliases.c.alias).where(
                entity_aliases.c.entity_id == row["id"]
            )
        )
        return Entity(
            id=EntityId(row["id"]),
            name=row["name"],
            entity_type=EntityType(row["entity_type"]),
            aliases=[a["alias"] for a in aliases],
            is_provisional=bool(row["is_provisional"]),
        )


class PostgresMemoryRepository:
    """Facts, events, and relationships.

    Takes a ClockPort because `created_at` is NOT NULL on all three tables and means
    "when this row was written" — which is not derivable from the domain objects.
    Deriving it from a temporal field, as an earlier version did, produced NULL for
    any record without a resolved date and would have failed every insert.
    """

    def __init__(self, store: RelationalStorePort, clock: ClockPort) -> None:
        self._store = store
        self._clock = clock

    async def insert_fact(
        self,
        fact: Fact,
        salience_category: str | None,
        tx: Transaction | None = None,
    ) -> Fact:
        expression = fact.temporal_expression
        async with scope(self._store, tx) as t:
            await t.execute(
                insert(facts).values(
                    id=fact.id,
                    statement=fact.statement,
                    origin=fact.origin.value,
                    confidence=fact.confidence.value,
                    salience=fact.salience,
                    salience_category=salience_category,
                    valid_from=fact.validity.valid_from,
                    valid_to=fact.validity.valid_to,
                    asserted_at=fact.belief.asserted_at,
                    retracted_at=fact.belief.retracted_at,
                    temporal_raw_phrase=expression.raw_phrase if expression else None,
                    temporal_granularity=(
                        expression.granularity.value if expression else None
                    ),
                    temporal_method=expression.method.value if expression else None,
                    temporal_anchor_zone=expression.anchor_zone if expression else None,
                    superseded_by=fact.superseded_by,
                    created_at=self._clock.now(),
                )
            )
            for entity_id in fact.subject_entity_ids:
                await t.execute(
                    insert(fact_subjects).values(fact_id=fact.id, entity_id=entity_id)
                )
        return fact

    async def insert_event(
        self,
        event: Event,
        salience_category: str | None,
        tx: Transaction | None = None,
    ) -> Event:
        expression = event.temporal_expression
        async with scope(self._store, tx) as t:
            await t.execute(
                insert(events).values(
                    id=event.id,
                    description=event.description,
                    origin=event.origin.value,
                    salience=event.salience,
                    salience_category=salience_category,
                    occurred_at=event.occurred_at,
                    occurred_through=event.occurred_through,
                    temporal_raw_phrase=expression.raw_phrase if expression else None,
                    temporal_granularity=(
                        expression.granularity.value if expression else None
                    ),
                    temporal_method=expression.method.value if expression else None,
                    temporal_anchor_zone=expression.anchor_zone if expression else None,
                    created_at=self._clock.now(),
                )
            )
            for entity_id in event.participant_entity_ids:
                await t.execute(
                    insert(event_participants).values(
                        event_id=event.id, entity_id=entity_id
                    )
                )
        return event

    async def insert_relationship(
        self, relationship: Relationship, tx: Transaction | None = None
    ) -> Relationship:
        async with scope(self._store, tx) as t:
            await t.execute(
                insert(relationships).values(
                    id=relationship.id,
                    from_entity_id=relationship.from_entity_id,
                    to_entity_id=relationship.to_entity_id,
                    relation_type=relationship.relation_type,
                    origin=relationship.origin.value,
                    valid_from=relationship.validity.valid_from,
                    valid_to=relationship.validity.valid_to,
                    # From the clock, never from a temporal field. Deriving this from
                    # validity.valid_from produced NULL for undated relationships and
                    # failed every insert with NotNullViolationError.
                    created_at=self._clock.now(),
                )
            )
        return relationship

    async def get_fact(self, memory_id: MemoryId) -> Fact | None:
        row = await self._store.fetch_one(select(facts).where(facts.c.id == memory_id))
        return await self._hydrate_fact(row) if row else None

    async def active_facts(self, limit: int = 100) -> Sequence[Fact]:
        """Currently believed, not superseded, highest salience first.

        Salience ordering is what stops aggressively-extracted trivia from crowding
        out signal (ADR-017).
        """
        rows = await self._store.fetch_all(
            select(facts)
            .where(and_(facts.c.retracted_at.is_(None), facts.c.superseded_by.is_(None)))
            .order_by(facts.c.salience.desc(), facts.c.created_at.desc())
            .limit(limit)
        )
        # Batched hydration: one provenance query and one subjects query for the whole
        # page, rather than two per fact.
        return await self._hydrate_facts(rows)

    async def facts_for_entity(
        self, entity_id: EntityId, limit: int = 50
    ) -> Sequence[Fact]:
        owners = (
            select(fact_subjects.c.fact_id)
            .where(fact_subjects.c.entity_id == entity_id)
            .scalar_subquery()
        )
        rows = await self._store.fetch_all(
            select(facts)
            .where(
                and_(
                    facts.c.id.in_(owners),
                    facts.c.retracted_at.is_(None),
                    facts.c.superseded_by.is_(None),
                )
            )
            .order_by(facts.c.salience.desc())
            .limit(limit)
        )
        return await self._hydrate_facts(rows)

    async def relationships_for_entity(
        self, entity_id: EntityId
    ) -> Sequence[Relationship]:
        rows = await self._store.fetch_all(
            select(relationships).where(
                and_(
                    or_(
                        relationships.c.from_entity_id == entity_id,
                        relationships.c.to_entity_id == entity_id,
                    ),
                    relationships.c.retracted_at.is_(None),
                )
            )
        )
        return [
            Relationship(
                id=MemoryId(row["id"]),
                from_entity_id=EntityId(row["from_entity_id"]),
                to_entity_id=EntityId(row["to_entity_id"]),
                relation_type=row["relation_type"],
                origin=Origin(row["origin"]),
                provenance=[],
                validity=TemporalValidity(
                    valid_from=row["valid_from"], valid_to=row["valid_to"]
                ),
            )
            for row in rows
        ]

    async def count_facts(self) -> int:
        row = await self._store.fetch_one(select(func.count()).select_from(facts))
        return int(next(iter(row.values()))) if row else 0

    # -------------------------------------------------------- evolution (Unit 3)

    async def end_belief(
        self,
        memory_id: MemoryId,
        retracted_at: datetime,
        tx: Transaction | None = None,
    ) -> None:
        """Belief axis. The system stops believing this; world validity untouched."""
        async with scope(self._store, tx) as t:
            await t.execute(
                update(facts)
                .where(and_(facts.c.id == memory_id, facts.c.retracted_at.is_(None)))
                .values(retracted_at=retracted_at)
            )

    async def end_validity(
        self,
        memory_id: MemoryId,
        valid_to: datetime,
        tx: Transaction | None = None,
    ) -> None:
        """World axis. It stopped being true; the system still believes it once was."""
        async with scope(self._store, tx) as t:
            await t.execute(
                update(facts)
                .where(facts.c.id == memory_id)
                .values(valid_to=valid_to)
            )

    async def update_statement(
        self,
        memory_id: MemoryId,
        statement: str,
        tx: Transaction | None = None,
    ) -> None:
        async with scope(self._store, tx) as t:
            await t.execute(
                update(facts).where(facts.c.id == memory_id).values(statement=statement)
            )

    async def link_supersession(
        self,
        original_id: MemoryId,
        replacement_id: MemoryId,
        tx: Transaction | None = None,
    ) -> None:
        async with scope(self._store, tx) as t:
            await t.execute(
                update(facts)
                .where(facts.c.id == original_id)
                .values(superseded_by=replacement_id)
            )
            await t.execute(
                update(facts)
                .where(facts.c.id == replacement_id)
                .values(supersedes=original_id)
            )

    async def link_correction(
        self,
        original_id: MemoryId,
        replacement_id: MemoryId,
        tx: Transaction | None = None,
    ) -> None:
        async with scope(self._store, tx) as t:
            await t.execute(
                update(facts)
                .where(facts.c.id == replacement_id)
                .values(corrected_from=original_id)
            )

    async def facts_valid_at(self, when: datetime, limit: int = 200) -> Sequence[Fact]:
        """World-time query: true at `when`, according to what we believe now.

        NULL `valid_from` means the start is unknown, not that the fact began at the
        epoch. Such facts are included, because refusing to report an undated fact as
        currently true would hide most of what the system knows — ADR-010 leaves dates
        null rather than fabricating them, so nulls are the common case.
        """
        rows = await self._store.fetch_all(
            select(facts)
            .where(
                and_(
                    facts.c.retracted_at.is_(None),
                    or_(facts.c.valid_from.is_(None), facts.c.valid_from <= when),
                    or_(facts.c.valid_to.is_(None), facts.c.valid_to > when),
                )
            )
            .order_by(facts.c.salience.desc(), facts.c.created_at.desc())
            .limit(limit)
        )
        return await self._hydrate_facts(rows)

    async def facts_asserted_between(
        self, start: datetime, end: datetime
    ) -> Sequence[Fact]:
        rows = await self._store.fetch_all(
            select(facts)
            .where(and_(facts.c.asserted_at > start, facts.c.asserted_at <= end))
            .order_by(facts.c.asserted_at)
        )
        return await self._hydrate_facts(rows)

    async def _load_provenance(
        self, fact_ids: Sequence[MemoryId]
    ) -> dict[MemoryId, list[ProvenanceRef]]:
        """Batch-load provenance for a set of facts.

        One query for the whole set rather than one per fact. An earlier version
        skipped this and synthesised a placeholder reference pointing at the fact's
        own id as though it were an episode id — a fabrication that would have made
        `fact.provenance` actively misleading, in exactly the area (traceability)
        where the product's credibility rests.
        """
        if not fact_ids:
            return {}

        rows = await self._store.fetch_all(
            select(provenance_index).where(
                and_(
                    provenance_index.c.memory_id.in_(list(fact_ids)),
                    provenance_index.c.memory_kind == MemoryKind.FACT.value,
                )
            )
        )
        found: dict[MemoryId, list[ProvenanceRef]] = {}
        for row in rows:
            found.setdefault(MemoryId(row["memory_id"]), []).append(
                ProvenanceRef(
                    episode_id=EpisodeId(row["episode_id"]),
                    conversation_id=row["conversation_id"],
                    message_id=row["message_id"],
                    document_id=row["document_id"],
                )
            )
        return found

    async def _hydrate_facts(self, rows: Sequence[Any]) -> list[Fact]:
        if not rows:
            return []

        ids = [MemoryId(row["id"]) for row in rows]
        provenance = await self._load_provenance(ids)

        subject_rows = await self._store.fetch_all(
            select(fact_subjects).where(fact_subjects.c.fact_id.in_(ids))
        )
        subjects: dict[MemoryId, list[EntityId]] = {}
        for row in subject_rows:
            subjects.setdefault(MemoryId(row["fact_id"]), []).append(
                EntityId(row["entity_id"])
            )

        hydrated: list[Fact] = []
        for row in rows:
            fact_id = MemoryId(row["id"])
            refs = provenance.get(fact_id, [])
            if not refs:
                # A fact with no provenance row should be impossible: commit writes
                # both. If it happens, the record is untrustworthy rather than merely
                # incomplete, so it is skipped loudly instead of being returned with
                # invented traceability.
                _log.error(
                    "fact_without_provenance_skipped",
                    fact_id=str(fact_id),
                    consequence="record excluded from results; investigate the commit path",
                )
                continue

            hydrated.append(
                Fact(
                    id=fact_id,
                    statement=row["statement"],
                    origin=Origin(row["origin"]),
                    confidence=Confidence(row["confidence"]),
                    validity=TemporalValidity(
                        valid_from=row["valid_from"], valid_to=row["valid_to"]
                    ),
                    belief=BeliefWindow(
                        asserted_at=row["asserted_at"], retracted_at=row["retracted_at"]
                    ),
                    provenance=refs,
                    salience=float(row["salience"]),
                    subject_entity_ids=subjects.get(fact_id, []),
                    temporal_expression=_temporal_expression(row),
                    superseded_by=(
                        MemoryId(row["superseded_by"]) if row["superseded_by"] else None
                    ),
                )
            )
        return hydrated

    async def _hydrate_fact(self, row: Any) -> Fact | None:
        hydrated = await self._hydrate_facts([row])
        return hydrated[0] if hydrated else None


class PostgresProvenanceRepository:
    def __init__(self, store: RelationalStorePort) -> None:
        self._store = store

    async def record(
        self,
        memory_id: MemoryId,
        memory_kind: MemoryKind,
        ref: ProvenanceRef,
        recorded_at: datetime,
        tx: Transaction | None = None,
    ) -> None:
        async with scope(self._store, tx) as t:
            # Idempotent. A retried commit must not inflate the corroboration count,
            # or a single-source fact would look corroborated and survive a deletion
            # that should have retracted it (ADR-012).
            existing = await t.fetch_one(
                select(provenance_index).where(
                    and_(
                        provenance_index.c.memory_id == memory_id,
                        provenance_index.c.memory_kind == memory_kind.value,
                        provenance_index.c.episode_id == ref.episode_id,
                    )
                )
            )
            if existing is not None:
                return

            await t.execute(
                insert(provenance_index).values(
                    memory_id=memory_id,
                    memory_kind=memory_kind.value,
                    episode_id=ref.episode_id,
                    conversation_id=ref.conversation_id,
                    message_id=ref.message_id,
                    document_id=ref.document_id,
                    recorded_at=recorded_at,
                )
            )

    async def for_memory(
        self, memory_id: MemoryId, memory_kind: MemoryKind
    ) -> Sequence[ProvenanceRef]:
        rows = await self._store.fetch_all(
            select(provenance_index).where(
                and_(
                    provenance_index.c.memory_id == memory_id,
                    provenance_index.c.memory_kind == memory_kind.value,
                )
            )
        )
        return [
            ProvenanceRef(
                episode_id=EpisodeId(row["episode_id"]),
                conversation_id=row["conversation_id"],
                message_id=row["message_id"],
                document_id=row["document_id"],
            )
            for row in rows
        ]

    async def count_for_memory(
        self, memory_id: MemoryId, memory_kind: MemoryKind
    ) -> int:
        row = await self._store.fetch_one(
            select(func.count())
            .select_from(provenance_index)
            .where(
                and_(
                    provenance_index.c.memory_id == memory_id,
                    provenance_index.c.memory_kind == memory_kind.value,
                )
            )
        )
        return int(next(iter(row.values()))) if row else 0

    async def memories_from_episode(
        self, episode_id: EpisodeId
    ) -> Sequence[tuple[MemoryId, MemoryKind]]:
        rows = await self._store.fetch_all(
            select(
                provenance_index.c.memory_id, provenance_index.c.memory_kind
            ).where(provenance_index.c.episode_id == episode_id)
        )
        return [
            (MemoryId(row["memory_id"]), MemoryKind(row["memory_kind"])) for row in rows
        ]
