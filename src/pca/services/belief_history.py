"""BeliefHistoryService — what the system believed, and when (FR-04.8, FR-05.5).

Layer L3.

This service owns the belief axis. Its counterpart on the world axis is
`TimelineService.state_at`, and the difference between the two answers for the same
instant is the observable evidence that the system tracks both.

    state_at(March)     what was TRUE in March, per what we believe now
    believed_at(March)  what we THOUGHT in March, including things since corrected

A worked example, because the distinction is easy to nod along to and then implement
wrongly:

    1. In March the user says "Priya works at Google". Believed from March.
    2. In June they say "sorry, I meant Microsoft — she never worked at Google".

    state_at(March)    -> "Priya works at Microsoft"
                          The Google fact was never true, so it is absent from the
                          world timeline entirely.
    believed_at(March) -> "Priya works at Google"
                          Because that is genuinely what the system thought at the
                          time, and pretending otherwise makes the audit trail
                          useless.

Belief windows for a single memory never overlap. Every transition closes the previous
window before opening the next, so `believed_at` returns exactly one belief per memory
per instant. Overlapping windows would let it return two contradictory answers.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import uuid4

from pca.domain.enums import BeliefChangeCause, MemoryKind
from pca.domain.history import BeliefTransition
from pca.domain.ids import MemoryId
from pca.domain.memory import Fact
from pca.domain.temporal import BeliefWindow, TemporalValidity
from pca.observability.logging import get_logger
from pca.ports.clock import ClockPort
from pca.ports.repositories import BeliefRepositoryPort
from pca.ports.store import Transaction

_log = get_logger(__name__)


class BeliefHistoryService:
    def __init__(self, repository: BeliefRepositoryPort, clock: ClockPort) -> None:
        self._repository = repository
        self._clock = clock

    # ------------------------------------------------------------------- writes

    async def record_assertion(
        self, fact: Fact, tx: Transaction | None = None
    ) -> BeliefTransition:
        """Open the first belief window for a newly committed fact."""
        return await self._record(
            memory_id=fact.id,
            memory_kind=MemoryKind.FACT,
            cause=BeliefChangeCause.ASSERTED,
            statement=fact.statement,
            validity=fact.validity,
            asserted_at=fact.belief.asserted_at,
            tx=tx,
        )

    async def record_correction(
        self,
        original: Fact,
        corrected_statement: str,
        replacement_id: MemoryId,
        reason: str,
        tx: Transaction | None = None,
    ) -> BeliefTransition:
        """Close the mistaken belief and open the corrected one.

        The closed window keeps the ORIGINAL statement, snapshotted. That snapshot is
        the only remaining trace of what the system used to think, since the fact row
        itself has been superseded by the replacement.
        """
        now = self._clock.now()
        await self._repository.close_open_transition(
            original.id, MemoryKind.FACT, now, tx=tx
        )
        await self._record(
            memory_id=original.id,
            memory_kind=MemoryKind.FACT,
            cause=BeliefChangeCause.CORRECTED,
            statement=original.statement,
            validity=original.validity,
            asserted_at=original.belief.asserted_at,
            retracted_at=now,
            superseded_by=replacement_id,
            reason=reason,
            tx=tx,
        )
        return await self._record(
            memory_id=replacement_id,
            memory_kind=MemoryKind.FACT,
            cause=BeliefChangeCause.ASSERTED,
            statement=corrected_statement,
            # World validity carries over untouched. A correction says the system
            # recorded the wrong thing, not that the world changed — so the period the
            # fact refers to is unchanged.
            validity=original.validity,
            asserted_at=now,
            reason=reason,
            tx=tx,
        )

    async def record_supersession(
        self,
        original: Fact,
        replacement_id: MemoryId,
        replacement_statement: str,
        effective_from: datetime,
        tx: Transaction | None = None,
    ) -> BeliefTransition:
        """Record that the world changed.

        Note what does NOT happen here: the original's belief window is not closed.
        We still believe the old fact was true for its window, and closing the window
        would erase the earlier state that FR-04.4 requires be retained.

        What changes is the original's world validity, which now ends at
        `effective_from`. The snapshot is re-recorded with that bound so the belief
        trail shows the narrowing.
        """
        now = self._clock.now()
        await self._repository.close_open_transition(
            original.id, MemoryKind.FACT, now, tx=tx
        )
        await self._record(
            memory_id=original.id,
            memory_kind=MemoryKind.FACT,
            cause=BeliefChangeCause.SUPERSEDED,
            statement=original.statement,
            validity=TemporalValidity(
                valid_from=original.validity.valid_from, valid_to=effective_from
            ),
            asserted_at=original.belief.asserted_at,
            retracted_at=now,
            superseded_by=replacement_id,
            tx=tx,
        )
        # Re-opened immediately with the bounded validity: still believed, now with a
        # known end in world time.
        await self._record(
            memory_id=original.id,
            memory_kind=MemoryKind.FACT,
            cause=BeliefChangeCause.ASSERTED,
            statement=original.statement,
            validity=TemporalValidity(
                valid_from=original.validity.valid_from, valid_to=effective_from
            ),
            asserted_at=now,
            tx=tx,
        )
        return await self._record(
            memory_id=replacement_id,
            memory_kind=MemoryKind.FACT,
            cause=BeliefChangeCause.ASSERTED,
            statement=replacement_statement,
            validity=TemporalValidity(valid_from=effective_from, valid_to=None),
            asserted_at=now,
            tx=tx,
        )

    async def record_retraction(
        self,
        fact: Fact,
        reason: str,
        cause: BeliefChangeCause = BeliefChangeCause.RETRACTED,
        tx: Transaction | None = None,
    ) -> BeliefTransition:
        """Close a belief window with no replacement."""
        now = self._clock.now()
        await self._repository.close_open_transition(
            fact.id, MemoryKind.FACT, now, tx=tx
        )
        return await self._record(
            memory_id=fact.id,
            memory_kind=MemoryKind.FACT,
            cause=cause,
            statement=fact.statement,
            validity=fact.validity,
            asserted_at=fact.belief.asserted_at,
            retracted_at=now,
            reason=reason,
            tx=tx,
        )

    # -------------------------------------------------------------------- reads

    async def believed_at(
        self, when: datetime, limit: int = 200
    ) -> Sequence[BeliefTransition]:
        """What the system believed at `when`.

        Compare against `TimelineService.state_at` for the same instant. Where they
        disagree, the system was wrong and later found out.
        """
        return await self._repository.believed_at(when, limit)

    async def trail(self, memory_id: MemoryId) -> Sequence[BeliefTransition]:
        """Every belief ever held about one memory, oldest first."""
        return await self._repository.for_memory(memory_id, MemoryKind.FACT)

    async def count(self) -> int:
        return await self._repository.count()

    # --------------------------------------------------------------- internals

    async def _record(
        self,
        *,
        memory_id: MemoryId,
        memory_kind: MemoryKind,
        cause: BeliefChangeCause,
        statement: str,
        validity: TemporalValidity,
        asserted_at: datetime,
        retracted_at: datetime | None = None,
        superseded_by: MemoryId | None = None,
        reason: str | None = None,
        tx: Transaction | None = None,
    ) -> BeliefTransition:
        transition = BeliefTransition(
            id=MemoryId(uuid4()),
            memory_id=memory_id,
            memory_kind=memory_kind,
            cause=cause,
            belief=BeliefWindow(asserted_at=asserted_at, retracted_at=retracted_at),
            statement=statement,
            validity=validity,
            superseded_by=superseded_by,
            reason=reason,
            recorded_at=self._clock.now(),
        )
        return await self._repository.record(transition, tx=tx)
