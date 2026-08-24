"""MemoryService — the single write path into memory.

Layer L3.

**Unit 2 scope: `commit` only.** Unit 3 adds `correct`, `supersede`, and `retract`,
along with belief-window transitions and conflict integration. The class is
introduced here rather than as a throwaway writer so that Unit 3 extends it instead
of replacing it.

Commit ordering matters and is not arbitrary:

    1. resolve entities        so facts have subjects to attach to
    2. insert facts and events
    3. insert relationships    needs both endpoints resolved
    4. record provenance       every record traceable to its episode (FR-02.5)

Entity resolution runs through EntityService, which never merges silently (ADR-014).
A commit can therefore legitimately produce provisional duplicates, and the receipt
reports them so they can be reviewed rather than accumulating unseen.
"""

from __future__ import annotations

from uuid import uuid4

from pca.domain.conversation import Episode
from pca.domain.enums import EntityType, MemoryKind, ResolutionOutcome
from pca.domain.extraction import ExtractionCandidates
from pca.domain.ids import EntityId, MemoryId
from pca.domain.memory import (
    CommitReceipt,
    Event,
    Fact,
    ProvenanceRef,
    Relationship,
)
from pca.domain.temporal import BeliefWindow, TemporalValidity
from pca.observability.logging import get_logger
from pca.ports.clock import ClockPort
from pca.ports.repositories import MemoryRepositoryPort
from pca.services.entities import EntityService
from pca.services.provenance import ProvenanceService

_log = get_logger(__name__)


class MemoryService:
    def __init__(
        self,
        repository: MemoryRepositoryPort,
        entities: EntityService,
        provenance: ProvenanceService,
        clock: ClockPort,
    ) -> None:
        self._repository = repository
        self._entities = entities
        self._provenance = provenance
        self._clock = clock

    async def commit(
        self, candidates: ExtractionCandidates, episode: Episode
    ) -> CommitReceipt:
        """Persist extraction candidates as memory.

        Not transactional across the whole commit in Unit 2. That is a known gap:
        Unit 3 introduces the transaction boundary along with the belief-history and
        operation-log writes that must be atomic with the memory rows. Recorded
        rather than glossed over.
        """
        if candidates.is_empty:
            _log.info("commit_skipped_empty", episode_id=str(episode.id))
            return CommitReceipt(episode_id=episode.id)

        ref = ProvenanceRef(
            episode_id=episode.id,
            conversation_id=episode.conversation_id,
            message_id=episode.message_id,
            document_id=episode.document_id,
        )

        resolved, provisional = await self._resolve_entities(candidates)

        fact_ids = await self._commit_facts(candidates, resolved, ref)
        event_ids = await self._commit_events(candidates, resolved, ref)
        relationship_ids = await self._commit_relationships(candidates, resolved, ref)

        receipt = CommitReceipt(
            episode_id=episode.id,
            fact_ids=fact_ids,
            event_ids=event_ids,
            relationship_ids=relationship_ids,
            entity_ids=list(resolved.values()),
            provisional_entity_ids=provisional,
        )

        _log.info(
            "memory_committed",
            episode_id=str(episode.id),
            facts=len(fact_ids),
            events=len(event_ids),
            relationships=len(relationship_ids),
            entities=len(resolved),
            provisional_entities=len(provisional),
            needs_clarification=receipt.needs_clarification,
        )
        if provisional:
            # Warning, not info: an ambiguous entity means a fact may be attached to
            # the wrong person until someone decides. That is worth surfacing.
            _log.warning(
                "commit_created_provisional_entities",
                episode_id=str(episode.id),
                entity_ids=[str(e) for e in provisional],
                action_required="review via list_provisional and merge deliberately",
            )

        return receipt

    # --------------------------------------------------------------- internals

    async def _resolve_entities(
        self, candidates: ExtractionCandidates
    ) -> tuple[dict[str, EntityId], list[EntityId]]:
        """Resolve every mentioned name to an entity id.

        Collects names from the explicit entity list *and* from fact subjects, event
        participants, and relationship endpoints. The model does not always list an
        entity it then references, and a fact with an unresolvable subject would
        silently lose its connection to the person it is about.
        """
        types: dict[str, EntityType] = {}
        for candidate in candidates.entities:
            types[candidate.name] = candidate.entity_type

        mentioned: list[str] = [c.name for c in candidates.entities]
        for fact in candidates.facts:
            mentioned.extend(fact.subject_names)
        for event in candidates.events:
            mentioned.extend(event.participant_names)
        for relationship in candidates.relationships:
            mentioned.extend([relationship.from_name, relationship.to_name])

        resolved: dict[str, EntityId] = {}
        provisional: list[EntityId] = []
        seen: set[str] = set()

        for name in mentioned:
            key = name.strip()
            if not key or key.casefold() in seen:
                continue
            seen.add(key.casefold())

            decision = await self._entities.resolve_for_extraction(
                key, types.get(key, EntityType.OTHER)
            )
            resolved[key] = decision.entity.id
            if decision.outcome is ResolutionOutcome.PROVISIONAL:
                provisional.append(decision.entity.id)

        return resolved, provisional

    def _ids_for(self, names: list[str], resolved: dict[str, EntityId]) -> list[EntityId]:
        found: list[EntityId] = []
        for name in names:
            entity_id = resolved.get(name.strip())
            if entity_id is not None and entity_id not in found:
                found.append(entity_id)
        return found

    async def _commit_facts(
        self,
        candidates: ExtractionCandidates,
        resolved: dict[str, EntityId],
        ref: ProvenanceRef,
    ) -> list[MemoryId]:
        now = self._clock.now()
        ids: list[MemoryId] = []

        for candidate in candidates.facts:
            expression = candidate.temporal_expression
            fact = Fact(
                id=MemoryId(uuid4()),
                statement=candidate.statement,
                origin=candidate.origin,
                confidence=candidate.confidence,
                # World time comes from the resolved phrase when there is one. An
                # UNKNOWN granularity yields nulls, never a fabricated date (ADR-010).
                validity=TemporalValidity(
                    valid_from=expression.resolved_from if expression else None,
                    valid_to=None,
                ),
                # Belief time starts now. Distinct axis from world time (ADR-011).
                belief=BeliefWindow(asserted_at=now),
                provenance=[ref],
                salience=candidate.salience,
                subject_entity_ids=self._ids_for(candidate.subject_names, resolved),
                temporal_expression=expression,
            )
            stored = await self._repository.insert_fact(
                fact,
                salience_category=(
                    candidate.salience_category.value
                    if candidate.salience_category
                    else None
                ),
            )
            await self._provenance.record(stored.id, MemoryKind.FACT, ref)
            ids.append(stored.id)

        return ids

    async def _commit_events(
        self,
        candidates: ExtractionCandidates,
        resolved: dict[str, EntityId],
        ref: ProvenanceRef,
    ) -> list[MemoryId]:
        ids: list[MemoryId] = []

        for candidate in candidates.events:
            expression = candidate.temporal_expression
            event = Event(
                id=MemoryId(uuid4()),
                description=candidate.description,
                origin=candidate.origin,
                provenance=[ref],
                occurred_at=expression.resolved_from if expression else None,
                occurred_through=expression.resolved_to if expression else None,
                participant_entity_ids=self._ids_for(candidate.participant_names, resolved),
                temporal_expression=expression,
                salience=candidate.salience,
            )
            stored = await self._repository.insert_event(
                event,
                salience_category=(
                    candidate.salience_category.value
                    if candidate.salience_category
                    else None
                ),
            )
            await self._provenance.record(stored.id, MemoryKind.EVENT, ref)
            ids.append(stored.id)

        return ids

    async def _commit_relationships(
        self,
        candidates: ExtractionCandidates,
        resolved: dict[str, EntityId],
        ref: ProvenanceRef,
    ) -> list[MemoryId]:
        ids: list[MemoryId] = []

        for candidate in candidates.relationships:
            source = resolved.get(candidate.from_name.strip())
            target = resolved.get(candidate.to_name.strip())
            if source is None or target is None or source == target:
                # Dropped rather than failing the commit. An unresolvable endpoint
                # means the relationship cannot be expressed, but the facts alongside
                # it are still worth keeping.
                _log.warning(
                    "relationship_skipped",
                    relation=candidate.relation_type,
                    from_name=candidate.from_name,
                    to_name=candidate.to_name,
                    reason="endpoint unresolved or self-referential",
                )
                continue

            relationship = Relationship(
                id=MemoryId(uuid4()),
                from_entity_id=source,
                to_entity_id=target,
                relation_type=candidate.relation_type,
                origin=candidate.origin,
                provenance=[ref],
            )
            stored = await self._repository.insert_relationship(relationship)
            await self._provenance.record(stored.id, MemoryKind.RELATIONSHIP, ref)
            ids.append(stored.id)

        return ids
