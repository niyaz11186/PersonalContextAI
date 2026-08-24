"""In-memory fakes for the Unit 2 repository ports.

These mirror the real adapters' *semantics*, not just their signatures. In
particular `find_by_name` returns every match rather than a best one, because
ADR-014's whole point is that ambiguity must reach the caller intact. A fake that
helpfully returned one entity would hide the exact condition the policy exists for.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pca.domain.enums import EntityType, MemoryKind
from pca.domain.ids import EntityId, EpisodeId, MemoryId
from pca.domain.memory import Entity, Event, Fact, ProvenanceRef, Relationship


class FakeEntityRepository:
    def __init__(self) -> None:
        self.entities: dict[EntityId, Entity] = {}
        self.aliases: dict[EntityId, set[str]] = {}
        self.merged: dict[EntityId, tuple[EntityId, str, datetime]] = {}

    async def create(
        self,
        entity_id: EntityId,
        name: str,
        entity_type: EntityType,
        created_at: datetime,
        is_provisional: bool = False,
        aliases: Sequence[str] = (),
    ) -> Entity:
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

    async def add_aliases(self, entity_id: EntityId, aliases: Sequence[str]) -> None:
        self.aliases.setdefault(entity_id, set()).update(aliases)

    async def merge(
        self, keep: EntityId, absorb: EntityId, reason: str, merged_at: datetime
    ) -> None:
        self.merged[absorb] = (keep, reason, merged_at)

    async def count(self) -> int:
        return len([e for e in self.entities if e not in self.merged])


class FakeMemoryRepository:
    def __init__(self) -> None:
        self.facts: dict[MemoryId, Fact] = {}
        self.events: dict[MemoryId, Event] = {}
        self.relationships: dict[MemoryId, Relationship] = {}
        self.salience_categories: dict[MemoryId, str | None] = {}

    async def insert_fact(self, fact: Fact, salience_category: str | None) -> Fact:
        self.facts[fact.id] = fact
        self.salience_categories[fact.id] = salience_category
        return fact

    async def insert_event(self, event: Event, salience_category: str | None) -> Event:
        self.events[event.id] = event
        self.salience_categories[event.id] = salience_category
        return event

    async def insert_relationship(self, relationship: Relationship) -> Relationship:
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


class FakeProvenanceRepository:
    def __init__(self) -> None:
        self.rows: list[tuple[MemoryId, MemoryKind, ProvenanceRef, datetime]] = []

    async def record(
        self,
        memory_id: MemoryId,
        memory_kind: MemoryKind,
        ref: ProvenanceRef,
        recorded_at: datetime,
    ) -> None:
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
