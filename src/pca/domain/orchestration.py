"""Orchestration domain types.

Layer L0. Standard library only — no imports from any other pca package except
sibling L0 modules.

These carry the decisions the L2 workflows make, deliberately as data rather than as
control flow. The pattern that matters most here is `Degradation`: it pairs the
action a caller should take with the sentence the user must be shown. NFR-06.5 says
the system may degrade "with disclosure", and the most likely way to fail that
requirement is for a caller to read the action, act on it, and quietly drop the
disclosure. Binding the two together means dropping the text requires deliberately
ignoring a field rather than merely forgetting one.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pca.domain.enums import (
    ClarificationStatus,
    CorrectionStatus,
    DegradationAction,
    ExtractionState,
    Intent,
    OperationKind,
)
from pca.domain.ids import ConversationId, EpisodeId, MemoryId


@dataclass(frozen=True, slots=True)
class Degradation:
    """A dependency failed and the system is continuing anyway.

    `disclosure` is not optional and must not be empty. A degraded answer that reads
    exactly like a healthy one is worse than an error, because the user has no way to
    know the reply was built on incomplete memory.
    """

    action: DegradationAction
    disclosure: str
    cause: str

    def __post_init__(self) -> None:
        if not self.disclosure.strip():
            raise ValueError(
                "a Degradation must carry user-facing disclosure text (NFR-06.5)"
            )


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """Which workflow should handle this message, and how sure we are.

    `confidence` exists so that uncertainty has somewhere to go other than a guess.
    Below the router's threshold the intent is CLARIFY — asking is cheap, and
    answering a correction as though it were conversation is not.
    """

    intent: Intent
    confidence: float
    rationale: str
    consulted_model: bool = False

    @property
    def is_confident(self) -> bool:
        return self.confidence >= 0.6


@dataclass(frozen=True, slots=True)
class BarrierResult:
    """Outcome of waiting for a conversation's pending extraction (ADR-008)."""

    cleared: bool
    waited: timedelta
    pending_episodes: int = 0
    degradation: Degradation | None = None

    @property
    def timed_out(self) -> bool:
        return not self.cleared


@dataclass(frozen=True, slots=True)
class ExtractionRecord:
    """The durable status row, as the domain sees it."""

    episode_id: EpisodeId
    conversation_id: ConversationId | None
    state: ExtractionState
    attempts: int
    submitted_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    @property
    def is_in_flight(self) -> bool:
        return self.state in (ExtractionState.PENDING, ExtractionState.RUNNING)


@dataclass(frozen=True, slots=True)
class ExtractionOutcome:
    """Result of one background extraction run.

    `already_done` reports the idempotency path (ADR-008): a resubmitted episode is a
    no-op, not a failure, and not a second write.
    """

    episode_id: EpisodeId
    state: ExtractionState
    facts_committed: int = 0
    contradictions: list[str] = field(default_factory=list)
    needs_clarification: bool = False
    already_done: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CorrectionRequest:
    """A user assertion that something in memory is wrong or has changed.

    `memory_id` is optional because the user usually says "that's wrong" without
    naming a record; the workflow identifies candidates and confirms when more than
    one is affected rather than correcting whichever ranked first.
    """

    conversation_id: ConversationId
    statement: str
    reason: str
    memory_id: MemoryId | None = None


@dataclass(frozen=True, slots=True)
class CorrectionResult:
    """Outcome of running the correction workflow.

    `thread_id` is returned even when nothing was applied, because an
    AWAITING_CONFIRMATION result is resumable and the id is the only handle to it.
    """

    status: CorrectionStatus
    thread_id: str
    operation: OperationKind | None = None
    original_id: MemoryId | None = None
    replacement_id: MemoryId | None = None
    question: str | None = None
    options: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AmbiguityContext:
    """Something the system cannot resolve on its own authority (ADR-014).

    Raised by entity ambiguity and by low-confidence routing. The workflow that
    handles it may not write memory until the user answers.
    """

    conversation_id: ConversationId
    question: str
    options: list[str] = field(default_factory=list)
    episode_id: EpisodeId | None = None
    memory_id: MemoryId | None = None


@dataclass(frozen=True, slots=True)
class ClarificationOutcome:
    """State of an interrupted clarification.

    `thread_id` is the LangGraph checkpoint thread. It is returned to the caller
    because it is the only handle by which the interrupt can later be resumed —
    including from a different process after a restart.
    """

    thread_id: str
    status: ClarificationStatus
    question: str | None = None
    answer: str | None = None
    applied_memory_id: MemoryId | None = None


@dataclass(frozen=True, slots=True)
class HistoricalQuery:
    """A question about the past.

    `about_belief` is the routing decision that makes this workflow worth having.
    "What was true in March?" reads world time via TimelineService.state_at.
    "What did I think in March?" reads belief time via BeliefHistoryService.
    believed_at. They return different answers whenever a correction has landed in
    between, so answering the wrong one is confidently wrong rather than merely
    unhelpful.
    """

    conversation_id: ConversationId
    text: str
    start: datetime | None = None
    end: datetime | None = None
    about_belief: bool = False
