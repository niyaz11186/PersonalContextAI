"""MemoryService — the single write path into memory.

Layer L3.

Four operations, and the distinction between the middle two is the heart of Unit 3:

    commit      new memory from an episode
    correct     "that's not what I said"  -> the system recorded it WRONGLY
    supersede   "she moved in March"      -> the WORLD changed
    retract     drop a belief with no replacement

`correct` and `supersede` are not variations on a theme. They act on different time
axes and conflating them silently corrupts the timeline:

    correct    belief ENDS (retracted_at set). World validity untouched — the fact
               was never true, so there is no true period to preserve.

    supersede  belief CONTINUES. World validity ENDS (valid_to set). We still believe
               the old fact was true for its window; erasing it would destroy the
               historical state FR-04.4 requires be kept.

If supersession retracted the old belief, "where did Priya live before Pune?" would
have no answer. If correction left world validity in place, the system would claim a
fact it knows to be false was nonetheless true for a period.

Commit ordering is not arbitrary:

    1. resolve entities        so facts have subjects to attach to
    2. insert facts and events
    3. insert relationships    needs both endpoints resolved
    4. record provenance       every record traceable to its episode (FR-02.5)
    5. record belief + audit   atomic with the rows they describe

Everything in a commit now happens inside ONE transaction. Unit 2 wrote each row
independently, and a live commit demonstrated the consequence: facts and entities
landed, relationships failed, and the episode was left half-written with no signal
that anything was missing.

Entity resolution runs through EntityService, which never merges silently (ADR-014).
A commit can therefore legitimately produce provisional duplicates, and the receipt
reports them so they can be reviewed rather than accumulating unseen.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from uuid import uuid4

from pca.domain.conversation import Episode
from pca.domain.enums import (
    BeliefChangeCause,
    Confidence,
    EntityType,
    MemoryKind,
    OperationKind,
    Origin,
    ResolutionOutcome,
)
from pca.domain.errors import MemoryNotFound
from pca.domain.extraction import ExtractionCandidates
from pca.domain.history import CorrectionOutcome, SupersessionOutcome
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
from pca.ports.store import Transaction, TransactionManagerPort
from pca.services.belief_history import BeliefHistoryService
from pca.services.entities import EntityService
from pca.services.operation_log import MemoryOperationLog
from pca.services.provenance import ProvenanceService

_log = get_logger(__name__)


@asynccontextmanager
async def _joined(tx: Transaction):
    """Adapt an existing transaction to the context-manager shape.

    Lets one method body serve both "you gave me a transaction, use it" and "open your
    own", without the caller-visible branch appearing at every call site.
    """
    yield tx


class MemoryService:
    def __init__(
        self,
        repository: MemoryRepositoryPort,
        entities: EntityService,
        provenance: ProvenanceService,
        clock: ClockPort,
        transactions: TransactionManagerPort,
        beliefs: BeliefHistoryService,
        operations: MemoryOperationLog,
    ) -> None:
        self._repository = repository
        self._entities = entities
        self._provenance = provenance
        self._clock = clock
        self._transactions = transactions
        self._beliefs = beliefs
        self._operations = operations

    async def commit(
        self, candidates: ExtractionCandidates, episode: Episode
    ) -> CommitReceipt:
        """Persist extraction candidates as memory, atomically.

        One transaction spans entity resolution, memory rows, provenance, belief
        transitions, and the audit entry. Either the episode is fully recorded or it
        left no trace — there is no partial state for a later read to misinterpret.

        Graph ingestion deliberately happens *after* this returns. PostgreSQL is the
        durability point (ADR-005); the graph is a rebuildable projection.
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

        async with self._transactions.transaction() as tx:
            resolved, provisional = await self._resolve_entities(candidates, tx)

            fact_ids = await self._commit_facts(candidates, resolved, ref, tx)
            event_ids = await self._commit_events(candidates, resolved, ref, tx)
            relationship_ids = await self._commit_relationships(
                candidates, resolved, ref, tx
            )

            await self._operations.record(
                OperationKind.COMMIT,
                episode_id=episode.id,
                detail={
                    "facts": len(fact_ids),
                    "events": len(event_ids),
                    "relationships": len(relationship_ids),
                    "entities": len(resolved),
                },
                tx=tx,
            )

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

    # ------------------------------------------------------- evolution (Unit 3)

    async def correct(
        self,
        memory_id: MemoryId,
        corrected_statement: str,
        reason: str,
        origin: Origin = Origin.USER_STATED,
    ) -> CorrectionOutcome:
        """The system recorded something wrongly. Replace it.

        Belief axis only. The original's belief window closes; its world validity is
        left exactly as it was and carried onto the replacement, because a correction
        makes no claim about the world having changed — it says the record was wrong
        about a period that itself is unchanged.

        The original row is retained with `retracted_at` set rather than deleted. That
        is what lets `believed_at` still report the mistaken belief for a date before
        the correction, which is the difference between an audit trail and a rewrite.
        """
        async with self._transactions.transaction() as tx:
            original = await self._repository.get_fact(memory_id)
            if original is None:
                raise MemoryNotFound(f"no fact with id {memory_id}")

            now = self._clock.now()
            replacement = Fact(
                id=MemoryId(uuid4()),
                statement=corrected_statement,
                # Defaults to USER_STATED because a correction normally comes from the
                # user saying "that's wrong". This does NOT violate FR-02.7's ban on
                # promoting AI_INFERRED to USER_STATED: the original row keeps its own
                # origin and is retracted, while this is a distinct new fact whose
                # source genuinely is a user statement. Exposed as a parameter so a
                # system-initiated correction can declare AI_INFERRED honestly rather
                # than inheriting a default that would misattribute it.
                origin=origin,
                confidence=Confidence.CERTAIN,
                # World validity carried across untouched.
                validity=original.validity,
                belief=BeliefWindow(asserted_at=now),
                provenance=list(original.provenance),
                salience=original.salience,
                subject_entity_ids=list(original.subject_entity_ids),
                temporal_expression=original.temporal_expression,
            )

            await self._repository.insert_fact(
                replacement, salience_category=None, tx=tx
            )
            for ref in original.provenance:
                await self._provenance.record(
                    replacement.id, MemoryKind.FACT, ref, tx=tx
                )

            # Belief ends. Deliberately NOT end_validity — see the class docstring.
            await self._repository.end_belief(original.id, now, tx=tx)
            await self._repository.link_correction(original.id, replacement.id, tx=tx)

            await self._beliefs.record_correction(
                original=original,
                corrected_statement=corrected_statement,
                replacement_id=replacement.id,
                reason=reason,
                tx=tx,
            )
            await self._operations.record(
                OperationKind.CORRECT,
                memory_id=original.id,
                memory_kind=MemoryKind.FACT,
                reason=reason,
                detail={
                    "was": original.statement,
                    "now": corrected_statement,
                    "replacement_id": str(replacement.id),
                },
                tx=tx,
            )

        _log.info(
            "memory_corrected",
            original_id=str(original.id),
            replacement_id=str(replacement.id),
            reason=reason,
        )
        return CorrectionOutcome(
            original_id=original.id, replacement_id=replacement.id, reason=reason
        )

    async def supersede(
        self,
        memory_id: MemoryId,
        new_statement: str,
        effective_from: datetime,
        reason: str | None = None,
    ) -> SupersessionOutcome:
        """The world changed. Keep the old state, add the new one.

        World axis only. The original keeps its belief — we still think it was true —
        and gains a `valid_to` of `effective_from`. The replacement's validity starts
        there.

        This is what makes "where did Priya live before Pune?" answerable. Retracting
        the original instead would leave the timeline with a single state and no past.
        """
        async with self._transactions.transaction() as tx:
            original = await self._repository.get_fact(memory_id)
            if original is None:
                raise MemoryNotFound(f"no fact with id {memory_id}")

            now = self._clock.now()
            replacement = Fact(
                id=MemoryId(uuid4()),
                statement=new_statement,
                origin=original.origin,
                confidence=original.confidence,
                validity=TemporalValidity(valid_from=effective_from, valid_to=None),
                belief=BeliefWindow(asserted_at=now),
                provenance=list(original.provenance),
                salience=original.salience,
                subject_entity_ids=list(original.subject_entity_ids),
            )

            await self._repository.insert_fact(
                replacement, salience_category=None, tx=tx
            )
            for ref in original.provenance:
                await self._provenance.record(
                    replacement.id, MemoryKind.FACT, ref, tx=tx
                )

            # World validity ends. Belief is untouched, which is the entire difference
            # between this method and `correct`.
            await self._repository.end_validity(original.id, effective_from, tx=tx)
            await self._repository.link_supersession(
                original.id, replacement.id, tx=tx
            )

            await self._beliefs.record_supersession(
                original=original,
                replacement_id=replacement.id,
                replacement_statement=new_statement,
                effective_from=effective_from,
                tx=tx,
            )
            await self._operations.record(
                OperationKind.SUPERSEDE,
                memory_id=original.id,
                memory_kind=MemoryKind.FACT,
                reason=reason,
                detail={
                    "previous": original.statement,
                    "current": new_statement,
                    "effective_from": effective_from.isoformat(),
                    "replacement_id": str(replacement.id),
                },
                tx=tx,
            )

        _log.info(
            "memory_superseded",
            original_id=str(original.id),
            replacement_id=str(replacement.id),
            effective_from=effective_from.isoformat(),
        )
        return SupersessionOutcome(
            original_id=original.id,
            replacement_id=replacement.id,
            effective_from=effective_from,
        )

    async def retract(
        self,
        memory_id: MemoryId,
        reason: str,
        cause: BeliefChangeCause = BeliefChangeCause.RETRACTED,
        tx: Transaction | None = None,
    ) -> None:
        """Stop believing something, with no replacement.

        Accepts `tx` because source deletion (ADR-012) retracts several facts at once
        when their last supporting source disappears, and those retractions must land
        together with the deletion that caused them.
        """
        scope = _joined(tx) if tx is not None else self._transactions.transaction()
        async with scope as t:
            original = await self._repository.get_fact(memory_id)
            if original is None:
                raise MemoryNotFound(f"no fact with id {memory_id}")

            now = self._clock.now()
            await self._repository.end_belief(original.id, now, tx=t)
            await self._beliefs.record_retraction(
                original, reason=reason, cause=cause, tx=t
            )
            await self._operations.record(
                OperationKind.RETRACT,
                memory_id=original.id,
                memory_kind=MemoryKind.FACT,
                reason=reason,
                detail={"statement": original.statement, "cause": cause.value},
                tx=t,
            )

        _log.info(
            "memory_retracted",
            memory_id=str(memory_id),
            reason=reason,
            cause=cause.value,
        )

    # --------------------------------------------------------------- internals

    async def _resolve_entities(
        self, candidates: ExtractionCandidates, tx: Transaction | None = None
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
                key, types.get(key, EntityType.OTHER), tx=tx
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
        tx: Transaction | None = None,
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
                tx=tx,
            )
            await self._provenance.record(stored.id, MemoryKind.FACT, ref, tx=tx)
            # Opens the first belief window. Same transaction as the fact itself: a
            # fact with no belief history would be invisible to believed_at, so the
            # audit trail would silently omit it.
            await self._beliefs.record_assertion(stored, tx=tx)
            ids.append(stored.id)

        return ids

    async def _commit_events(
        self,
        candidates: ExtractionCandidates,
        resolved: dict[str, EntityId],
        ref: ProvenanceRef,
        tx: Transaction | None = None,
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
                tx=tx,
            )
            await self._provenance.record(stored.id, MemoryKind.EVENT, ref, tx=tx)
            ids.append(stored.id)

        return ids

    async def _commit_relationships(
        self,
        candidates: ExtractionCandidates,
        resolved: dict[str, EntityId],
        ref: ProvenanceRef,
        tx: Transaction | None = None,
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
            stored = await self._repository.insert_relationship(relationship, tx=tx)
            await self._provenance.record(
                stored.id, MemoryKind.RELATIONSHIP, ref, tx=tx
            )
            ids.append(stored.id)

        return ids
