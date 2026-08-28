"""In-memory fakes for the Unit 2 repository ports.

These mirror the real adapters' *semantics*, not just their signatures. In
particular `find_by_name` returns every match rather than a best one, because
ADR-014's whole point is that ambiguity must reach the caller intact. A fake that
helpfully returned one entity would hide the exact condition the policy exists for.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pca.domain.enums import EntityType, MemoryKind
from pca.domain.ids import EntityId, EpisodeId, MemoryId
from pca.domain.memory import Entity, Event, Fact, ProvenanceRef, Relationship
from pca.domain.temporal import BeliefWindow, TemporalValidity


class FakeEntityRepository:
    def __init__(self) -> None:
        self.entities: dict[EntityId, Entity] = {}
        self.aliases: dict[EntityId, set[str]] = {}
        self.merged: dict[EntityId, tuple[EntityId, str, datetime]] = {}
        # Every transaction object handed to a write. Lets a test assert that one
        # commit used exactly one transaction rather than several.
        self.write_transactions: list[Any] = []

    def snapshot(self) -> Any:
        return (
            dict(self.entities),
            {k: set(v) for k, v in self.aliases.items()},
            dict(self.merged),
        )

    def restore(self, snap: Any) -> None:
        self.entities, self.aliases, self.merged = (
            dict(snap[0]),
            {k: set(v) for k, v in snap[1].items()},
            dict(snap[2]),
        )

    async def create(
        self,
        entity_id: EntityId,
        name: str,
        entity_type: EntityType,
        created_at: datetime,
        is_provisional: bool = False,
        aliases: Sequence[str] = (),
        tx: Any | None = None,
    ) -> Entity:
        self.write_transactions.append(tx)
        entity = Entity(
            id=entity_id,
            name=name,
            entity_type=entity_type,
            aliases=list(aliases),
            is_provisional=is_provisional,
        )
        self.entities[entity_id] = entity
        self.aliases[entity_id] = set(aliases)
        return entity

    async def get(self, entity_id: EntityId) -> Entity | None:
        if entity_id in self.merged:
            return None
        return self.entities.get(entity_id)

    async def find_by_name(self, name: str) -> Sequence[Entity]:
        lowered = name.casefold()
        found: list[Entity] = []
        for entity_id, entity in self.entities.items():
            if entity_id in self.merged:
                continue  # absorbed entities must not be resolution candidates
            if entity.name.casefold() == lowered or any(
                a.casefold() == lowered for a in self.aliases.get(entity_id, set())
            ):
                found.append(
                    Entity(
                        id=entity.id,
                        name=entity.name,
                        entity_type=entity.entity_type,
                        aliases=sorted(self.aliases.get(entity_id, set())),
                        is_provisional=entity.is_provisional,
                    )
                )
        return found

    async def list_provisional(self, limit: int = 100) -> Sequence[Entity]:
        return [
            e
            for eid, e in self.entities.items()
            if e.is_provisional and eid not in self.merged
        ][:limit]

    async def add_aliases(
        self, entity_id: EntityId, aliases: Sequence[str], tx: Any | None = None
    ) -> None:
        self.write_transactions.append(tx)
        self.aliases.setdefault(entity_id, set()).update(aliases)

    async def merge(
        self,
        keep: EntityId,
        absorb: EntityId,
        reason: str,
        merged_at: datetime,
        tx: Any | None = None,
    ) -> None:
        self.write_transactions.append(tx)
        self.merged[absorb] = (keep, reason, merged_at)

    async def count(self) -> int:
        return len([e for e in self.entities if e not in self.merged])


class FakeMemoryRepository:
    def __init__(self, fail_on_relationship: bool = False) -> None:
        self.facts: dict[MemoryId, Fact] = {}
        self.events: dict[MemoryId, Event] = {}
        self.relationships: dict[MemoryId, Relationship] = {}
        self.salience_categories: dict[MemoryId, str | None] = {}
        self.corrected_from: dict[MemoryId, MemoryId] = {}
        self.supersedes: dict[MemoryId, MemoryId] = {}
        self.write_transactions: list[Any] = []
        # Reproduces the Unit 2 failure observed live: facts land, relationships
        # fail. Used to prove the commit is now atomic.
        self.fail_on_relationship = fail_on_relationship

    def snapshot(self) -> Any:
        return (
            dict(self.facts),
            dict(self.events),
            dict(self.relationships),
            dict(self.salience_categories),
            dict(self.corrected_from),
            dict(self.supersedes),
        )

    def restore(self, snap: Any) -> None:
        (
            self.facts,
            self.events,
            self.relationships,
            self.salience_categories,
            self.corrected_from,
            self.supersedes,
        ) = (dict(s) for s in snap)

    async def insert_fact(
        self, fact: Fact, salience_category: str | None, tx: Any | None = None
    ) -> Fact:
        self.write_transactions.append(tx)
        self.facts[fact.id] = fact
        self.salience_categories[fact.id] = salience_category
        return fact

    async def insert_event(
        self, event: Event, salience_category: str | None, tx: Any | None = None
    ) -> Event:
        self.write_transactions.append(tx)
        self.events[event.id] = event
        self.salience_categories[event.id] = salience_category
        return event

    async def insert_relationship(
        self, relationship: Relationship, tx: Any | None = None
    ) -> Relationship:
        self.write_transactions.append(tx)
        if self.fail_on_relationship:
            raise RuntimeError("simulated relationship insert failure")
        self.relationships[relationship.id] = relationship
        return relationship

    async def get_fact(self, memory_id: MemoryId) -> Fact | None:
        return self.facts.get(memory_id)

    async def active_facts(self, limit: int = 100) -> Sequence[Fact]:
        active = [f for f in self.facts.values() if f.is_active]
        active.sort(key=lambda f: f.salience, reverse=True)
        return active[:limit]

    async def facts_for_entity(
        self, entity_id: EntityId, limit: int = 50
    ) -> Sequence[Fact]:
        found = [
            f
            for f in self.facts.values()
            if entity_id in f.subject_entity_ids and f.is_active
        ]
        found.sort(key=lambda f: f.salience, reverse=True)
        return found[:limit]

    async def relationships_for_entity(
        self, entity_id: EntityId
    ) -> Sequence[Relationship]:
        return [
            r
            for r in self.relationships.values()
            if entity_id in (r.from_entity_id, r.to_entity_id)
        ]

    async def count_facts(self) -> int:
        return len(self.facts)

    # -------------------------------------------------------- evolution (Unit 3)

    async def end_belief(
        self, memory_id: MemoryId, retracted_at: datetime, tx: Any | None = None
    ) -> None:
        """Belief axis only. Mirrors the adapter's `retracted_at IS NULL` guard so a
        repeated correction cannot move an already-closed window."""
        self.write_transactions.append(tx)
        fact = self.facts.get(memory_id)
        if fact is None or fact.belief.retracted_at is not None:
            return
        self.facts[memory_id] = dataclasses.replace(
            fact,
            belief=BeliefWindow(
                asserted_at=fact.belief.asserted_at, retracted_at=retracted_at
            ),
        )

    async def end_validity(
        self, memory_id: MemoryId, valid_to: datetime, tx: Any | None = None
    ) -> None:
        """World axis only. Belief deliberately untouched."""
        self.write_transactions.append(tx)
        fact = self.facts.get(memory_id)
        if fact is None:
            return
        self.facts[memory_id] = dataclasses.replace(
            fact,
            validity=TemporalValidity(
                valid_from=fact.validity.valid_from, valid_to=valid_to
            ),
        )

    async def update_statement(
        self, memory_id: MemoryId, statement: str, tx: Any | None = None
    ) -> None:
        self.write_transactions.append(tx)
        fact = self.facts.get(memory_id)
        if fact is not None:
            self.facts[memory_id] = dataclasses.replace(fact, statement=statement)

    async def link_supersession(
        self,
        original_id: MemoryId,
        replacement_id: MemoryId,
        tx: Any | None = None,
    ) -> None:
        self.write_transactions.append(tx)
        original = self.facts.get(original_id)
        if original is not None:
            self.facts[original_id] = dataclasses.replace(
                original, superseded_by=replacement_id
            )
        self.supersedes[replacement_id] = original_id

    async def link_correction(
        self,
        original_id: MemoryId,
        replacement_id: MemoryId,
        tx: Any | None = None,
    ) -> None:
        self.write_transactions.append(tx)
        self.corrected_from[replacement_id] = original_id

    async def facts_valid_at(
        self, when: datetime, limit: int = 200
    ) -> Sequence[Fact]:
        """World-time query. Mirrors the adapter: a NULL bound is open, not epoch."""
        found = [
            f
            for f in self.facts.values()
            if f.belief.retracted_at is None
            and (f.validity.valid_from is None or f.validity.valid_from <= when)
            and (f.validity.valid_to is None or f.validity.valid_to > when)
        ]
        found.sort(key=lambda f: f.salience, reverse=True)
        return found[:limit]

    async def facts_asserted_between(
        self, start: datetime, end: datetime
    ) -> Sequence[Fact]:
        found = [
            f
            for f in self.facts.values()
            if start < f.belief.asserted_at <= end
        ]
        found.sort(key=lambda f: f.belief.asserted_at)
        return found


class FakeProvenanceRepository:
    def __init__(self) -> None:
        self.rows: list[tuple[MemoryId, MemoryKind, ProvenanceRef, datetime]] = []
        self.write_transactions: list[Any] = []

    def snapshot(self) -> Any:
        return list(self.rows)

    def restore(self, snap: Any) -> None:
        self.rows = list(snap)

    async def record(
        self,
        memory_id: MemoryId,
        memory_kind: MemoryKind,
        ref: ProvenanceRef,
        recorded_at: datetime,
        tx: Any | None = None,
    ) -> None:
        self.write_transactions.append(tx)
        # Primary key is (memory, kind, episode): re-recording the same source must
        # not inflate the corroboration count.
        for existing_id, existing_kind, existing_ref, _ in self.rows:
            if (
                existing_id == memory_id
                and existing_kind == memory_kind
                and existing_ref.episode_id == ref.episode_id
            ):
                return
        self.rows.append((memory_id, memory_kind, ref, recorded_at))

    async def for_memory(
        self, memory_id: MemoryId, memory_kind: MemoryKind
    ) -> Sequence[ProvenanceRef]:
        return [
            ref
            for mid, kind, ref, _ in self.rows
            if mid == memory_id and kind == memory_kind
        ]

    async def count_for_memory(self, memory_id: MemoryId, memory_kind: MemoryKind) -> int:
        return len(await self.for_memory(memory_id, memory_kind))

    async def memories_from_episode(
        self, episode_id: EpisodeId
    ) -> Sequence[tuple[MemoryId, MemoryKind]]:
        return [
            (mid, kind)
            for mid, kind, ref, _ in self.rows
            if ref.episode_id == episode_id
        ]
