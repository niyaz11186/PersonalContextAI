"""TimelineService — reconstructing the world axis (FR-04.5, FR-04.6, FR-04.7).

Layer L3.

This service answers questions about what was TRUE. `BeliefHistoryService` answers
questions about what was BELIEVED. Keeping them in separate services is deliberate:
they are easy to confuse, and a single class exposing both would invite calls to
whichever method name looked closest.

    TimelineService.state_at(March)         what was true in March
    BeliefHistoryService.believed_at(March) what we thought in March

`compare` returns both for the same instant, which is the only honest way to show a
user that the system was wrong and has since learned better.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from pca.domain.enums import BeliefChangeCause, MemoryKind
from pca.domain.history import BeliefTransition, TimelineDiff
from pca.domain.memory import Fact
from pca.observability.logging import get_logger
from pca.ports.clock import ClockPort
from pca.ports.repositories import BeliefRepositoryPort, MemoryRepositoryPort

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TimelineComparison:
    """Both axes at one instant.

    When `differs` is true the system held a belief at `when` that it no longer holds.
    That is not a bug to be smoothed over — it is the audit answer.
    """

    when: datetime
    was_true: list[str] = field(default_factory=list)
    was_believed: list[str] = field(default_factory=list)

    @property
    def differs(self) -> bool:
        return set(self.was_true) != set(self.was_believed)


class TimelineService:
    def __init__(
        self,
        memory: MemoryRepositoryPort,
        beliefs: BeliefRepositoryPort,
        clock: ClockPort,
    ) -> None:
        self._memory = memory
        self._beliefs = beliefs
        self._clock = clock

    async def state_at(self, when: datetime, limit: int = 200) -> Sequence[Fact]:
        """Facts true in the world at `when`, per current belief (FR-04.5).

        Excludes retracted facts entirely. A fact the system no longer believes was
        never true at any instant, so including it in a world-time reconstruction
        would be asserting something the system knows to be false.
        """
        return await self._memory.facts_valid_at(when, limit)

    async def believed_at(
        self, when: datetime, limit: int = 200
    ) -> Sequence[BeliefTransition]:
        """Beliefs held at `when` (FR-04.8). Delegates to the belief axis."""
        return await self._beliefs.believed_at(when, limit)

    async def compare(self, when: datetime, limit: int = 200) -> TimelineComparison:
        """Both axes side by side.

        The completion criterion for Unit 3 is that these two lists can differ for the
        same instant after a correction. If they never diverge, the system is storing
        one axis and labelling it two.
        """
        true_facts = await self.state_at(when, limit)
        believed = await self.believed_at(when, limit)
        comparison = TimelineComparison(
            when=when,
            was_true=[f.statement for f in true_facts],
            was_believed=[b.statement for b in believed],
        )
        if comparison.differs:
            _log.info(
                "timeline_axes_diverge",
                when=when.isoformat(),
                true_count=len(comparison.was_true),
                believed_count=len(comparison.was_believed),
                note="the system held a belief at this instant it no longer holds",
            )
        return comparison

    async def diff(self, start: datetime, end: datetime) -> TimelineDiff:
        """What changed between two instants (FR-04.6).

        Three buckets, not two. "Stopped being true" and "we were wrong about it" are
        different events with different implications, and collapsing them would tell a
        user their situation changed when in fact the record was simply fixed.
        """
        if end < start:
            raise ValueError("end must not precede start")

        at_start = {f.id: f for f in await self.state_at(start)}
        at_end = {f.id: f for f in await self.state_at(end)}

        became_true = [f.statement for fid, f in at_end.items() if fid not in at_start]

        # Corrections come from the BELIEF axis, not from comparing world state.
        # `state_at` excludes retracted facts, so a corrected fact is absent from both
        # endpoints — comparing them would report nothing at all. The belief history is
        # the only place that records the correction happened inside this window.
        correction_transitions = await self._beliefs.transitions_between(
            start, end, causes=(BeliefChangeCause.CORRECTED.value,)
        )
        corrected = [t.statement for t in correction_transitions]
        corrected_ids = {t.memory_id for t in correction_transitions}

        ceased = [
            fact.statement
            for fid, fact in at_start.items()
            if fid not in at_end and fid not in corrected_ids
        ]

        result = TimelineDiff(
            start=start,
            end=end,
            became_true=became_true,
            ceased_to_be_true=ceased,
            corrected=corrected,
        )
        _log.info(
            "timeline_diff",
            start=start.isoformat(),
            end=end.isoformat(),
            became_true=len(result.became_true),
            ceased=len(result.ceased_to_be_true),
            corrected=len(result.corrected),
        )
        return result

    async def evolution_of(self, statement_substring: str, limit: int = 50) -> list[str]:
        """How the system's view of something changed over time (FR-04.7).

        Matches on the snapshotted statements in belief history rather than on live
        facts, so a superseded or corrected phrasing is still findable.
        """
        needle = statement_substring.strip().casefold()
        if not needle:
            return []
        transitions = await self._beliefs.believed_at(self._clock.now(), limit)
        return [t.statement for t in transitions if needle in t.statement.casefold()]
