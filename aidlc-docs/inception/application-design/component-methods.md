# Component Methods

Method signatures and input/output types. Detailed business rules are deferred to Functional Design (per-unit, CONSTRUCTION phase).

All signatures are `async` unless noted. Types below are domain models, not Graphiti or SQLAlchemy types (layering rule 4).

---

## Core Domain Types

Sketched here to make the signatures meaningful. Full field-level definition happens in Functional Design.

```python
# --- Identity ---
ConversationId = NewType("ConversationId", UUID)
MessageId      = NewType("MessageId", UUID)
EpisodeId      = NewType("EpisodeId", UUID)
MemoryId       = NewType("MemoryId", UUID)
EntityId       = NewType("EntityId", UUID)

# --- Provenance and epistemic status ---
class Origin(StrEnum):
    USER_STATED = "user_stated"    # the user said this
    AI_INFERRED = "ai_inferred"    # the system derived this
    IMPORTED    = "imported"       # came from an imported document

class Confidence(StrEnum):
    CERTAIN   = "certain"
    PROBABLE  = "probable"
    UNCERTAIN = "uncertain"

# --- Bi-temporal envelope ---
@dataclass(frozen=True)
class TemporalValidity:
    """When the fact was true in the world."""
    valid_from: datetime | None
    valid_to:   datetime | None      # None = still true

@dataclass(frozen=True)
class BeliefWindow:
    """When the system believed the fact. Distinct from world-time."""
    asserted_at:  datetime
    retracted_at: datetime | None    # None = still believed

# --- Memory records ---
@dataclass(frozen=True)
class Fact:
    id: MemoryId
    statement: str
    origin: Origin
    confidence: Confidence
    validity: TemporalValidity
    belief: BeliefWindow
    subject_entity_ids: list[EntityId]
    provenance: ProvenanceRef
    superseded_by: MemoryId | None

@dataclass(frozen=True)
class Event:
    id: MemoryId
    description: str
    occurred_at: datetime | None
    occurred_through: datetime | None   # for periods
    participant_entity_ids: list[EntityId]
    origin: Origin
    provenance: ProvenanceRef

@dataclass(frozen=True)
class Entity:
    id: EntityId
    name: str
    entity_type: str            # person | organization | place | project | other
    aliases: list[str]
    attributes: list[Fact]      # attribute history, temporally scoped

@dataclass(frozen=True)
class Relationship:
    from_entity_id: EntityId
    to_entity_id: EntityId
    relation_type: str
    validity: TemporalValidity
    origin: Origin
    provenance: ProvenanceRef

@dataclass(frozen=True)
class ProvenanceRef:
    conversation_id: ConversationId | None
    message_id: MessageId | None
    document_id: UUID | None
    episode_id: EpisodeId

# --- Retrieval and context ---
@dataclass(frozen=True)
class RetrievalQuery:
    text: str
    time_range: tuple[datetime | None, datetime | None] | None
    entity_scope: list[EntityId] | None
    budget: RetrievalBudget

@dataclass(frozen=True)
class RetrievalResult:
    facts: list[Fact]
    events: list[Event]
    entities: list[Entity]
    relationships: list[Relationship]
    source_messages: list[Message]
    diagnostics: RetrievalDiagnostics   # what ran, what it cost, what was dropped

@dataclass(frozen=True)
class ContextPackage:
    """The four-way distinction required by FR-07.2 is structural, not a label."""
    user_stated: list[Fact]
    system_derived: list[Fact]
    currently_believed: list[Fact]
    uncertain: list[Fact]
    events: list[Event]
    entities: list[Entity]
    relationships: list[Relationship]
    timeline: Timeline | None
    conflicts: list[Conflict]
    source_excerpts: list[SourceExcerpt]
    degradation_notices: list[str]      # populated when NFR-06.5 applies
```

**Note on the two time axes.** `TemporalValidity` answers "when was this true?" and `BeliefWindow` answers "when did the system think so?". Keeping them separate is what makes FR-04.5 and FR-05.5 different questions with different answers. Collapsing them into one timestamp is the most likely way to get temporal correctness wrong.

---

## L2 Orchestration

### IntentRouter

```python
async def classify(self, message: str, conversation_id: ConversationId) -> RoutingDecision
```
Returns the target workflow plus a confidence. A low-confidence decision routes to `ClarificationWorkflow` rather than guessing.

### ConversationWorkflow

```python
async def run(self, message_id: MessageId) -> AsyncIterator[ResponseChunk]
```
Streams the response. Internally: retrieve, assemble, generate.

### ExtractionWorkflow

```python
async def run(self, episode_id: EpisodeId) -> ExtractionOutcome
```
Idempotent by `episode_id` (ADR-008).

### CorrectionWorkflow

```python
async def run(self, correction: CorrectionRequest) -> CorrectionOutcome
```

### HistoricalAnalysisWorkflow

```python
async def run(self, query: HistoricalQuery) -> AsyncIterator[ResponseChunk]
```

### ClarificationWorkflow

```python
async def run(self, ambiguity: AmbiguityContext) -> ClarificationOutcome
async def resume(self, thread_id: str, user_answer: str) -> ClarificationOutcome
```
`resume` exists because the workflow interrupts and waits. This pair is the concrete justification for LangGraph in ADR-006.

---

## L3 Domain Services

### ConversationService

```python
async def create_conversation(self, title: str | None = None) -> Conversation
async def append_message(self, conversation_id: ConversationId, role: Role, content: str) -> Message
async def get_history(self, conversation_id: ConversationId, limit: int | None = None) -> list[Message]
async def list_conversations(self, page: Page) -> Paged[Conversation]
```
No update or delete for messages. Append-only enforces FR-01.4.

### ExtractionCoordinator

```python
async def submit(self, episode_id: EpisodeId, conversation_id: ConversationId) -> None
async def await_barrier(self, conversation_id: ConversationId, timeout: timedelta) -> BarrierResult
async def recover_pending(self) -> list[EpisodeId]
```
`recover_pending` runs at startup and returns episodes left in-flight by a crash (ADR-008 durability constraint).

### ExtractionService

```python
async def extract(self, episode: Episode) -> ExtractionCandidates
```
Returns candidates only. It does not write. Separating extraction from commitment is what allows conflict detection to run in between.

### MemoryService

```python
async def commit(self, candidates: ExtractionCandidates, resolutions: list[ConflictResolution]) -> CommitReceipt
async def correct(self, memory_id: MemoryId, corrected: str, reason: str) -> Fact
async def supersede(self, memory_id: MemoryId, replacement: Fact, effective_from: datetime) -> Fact
async def retract(self, memory_id: MemoryId, reason: str) -> None
```
The only write path into memory. `correct` and `supersede` are distinct: correcting means the system recorded it wrong, superseding means the world changed. Both preserve history; only the reason and the temporal effect differ.

### ConflictDetectionService

```python
async def detect(self, candidates: ExtractionCandidates) -> list[Conflict]
```
Classifies each conflict as `AGREEMENT`, `REFINEMENT`, `TEMPORAL_CHANGE`, or `CONTRADICTION`. Only `CONTRADICTION` requires surfacing to the user.

### RetrievalService

```python
async def retrieve(self, query: RetrievalQuery) -> RetrievalResult
```

### RetrievalBudgetGovernor

```python
def budget_for(self, intent: Intent) -> RetrievalBudget
def should_continue(self, spent: Spend, gathered: int) -> bool
```
Not async — pure policy. Makes the stop condition explicit and testable rather than buried in retrieval code.

### ContextAssemblyService

```python
async def assemble(self, result: RetrievalResult, conversation_id: ConversationId) -> ContextPackage
def render(self, package: ContextPackage) -> str
```
`render` produces the prompt text. Kept separate from assembly so prompt formatting can be changed and evaluated without touching retrieval.

### TimelineService

```python
async def reconstruct(self, entity_id: EntityId | None, window: tuple[datetime, datetime]) -> Timeline
async def state_at(self, when: datetime, entity_id: EntityId | None = None) -> list[Fact]
async def diff(self, start: datetime, end: datetime) -> TimelineDiff
```
`state_at` filters on `TemporalValidity` (FR-04.5).

### BeliefHistoryService

```python
async def record(self, memory_id: MemoryId, belief: BeliefWindow, cause: BeliefChangeCause) -> None
async def believed_at(self, when: datetime) -> list[Fact]
async def belief_trail(self, memory_id: MemoryId) -> list[BeliefTransition]
```
`believed_at` filters on `BeliefWindow` (FR-05.5). Compare with `TimelineService.state_at` — same shape, different time axis, different answer.

### EntityService

```python
async def resolve(self, mention: str) -> list[EntityMatch]
async def get(self, entity_id: EntityId) -> Entity
async def attribute_history(self, entity_id: EntityId, attribute: str) -> list[Fact]
async def merge(self, keep: EntityId, absorb: EntityId, reason: str) -> Entity
```
`merge` is reversible via the operation log; it records rather than destroys.

### ProvenanceService

```python
async def chain(self, memory_id: MemoryId) -> ProvenanceChain
async def source_excerpt(self, ref: ProvenanceRef, window: int = 2) -> SourceExcerpt
```
`source_excerpt` returns surrounding messages so the user sees context, not an isolated sentence.

### DeletionService

```python
async def logical_delete(self, memory_id: MemoryId, reason: str) -> None
async def hard_delete(self, target: DeletionTarget, confirmation: str) -> DeletionReport
```
`hard_delete` requires explicit confirmation because it is irreversible and crosses both stores. This is the one operation in the system that genuinely destroys history, so it is deliberately awkward to call.

### ReindexService

```python
async def rebuild(self, since: datetime | None = None, resume_token: str | None = None) -> ReindexReport
async def verify(self) -> ConsistencyReport
```
Resumable per ADR-005 consequences. `verify` compares PostgreSQL episode count and checksums against graph state.

### ImportService

```python
async def import_text(self, content: str, source_name: str, stated_date: datetime | None) -> ImportReceipt
```
`stated_date` matters: an imported journal entry describes events at its own date, not at import time. Without this, imported history collapses onto today.

### ExportService / BackupService

```python
async def export_all(self, fmt: ExportFormat) -> ExportArtifact
async def backup(self) -> BackupArtifact
async def restore(self, artifact: BackupArtifact, confirmation: str) -> RestoreReport
```

---

## L4 Ports

### MemoryGraphPort

```python
async def add_episode(self, episode: Episode) -> GraphIngestResult
async def search_semantic(self, text: str, limit: int) -> list[GraphHit]
async def search_fulltext(self, text: str, limit: int) -> list[GraphHit]
async def search_by_entity(self, entity_id: EntityId, limit: int) -> list[GraphHit]
async def traverse(self, seed: EntityId, depth: int, edge_types: list[str] | None) -> list[GraphHit]
async def search_temporal(self, window: tuple[datetime, datetime], limit: int) -> list[GraphHit]
async def rerank(self, query: str, hits: list[GraphHit]) -> list[GraphHit]
async def invalidate_edge(self, edge_id: str, at: datetime) -> None
async def clear_all(self) -> None
```
`clear_all` exists to serve reindex. Its presence is only safe because PostgreSQL is the system of record (ADR-005).

### LLMProviderPort

```python
async def complete(self, prompt: Prompt, *, model: str) -> str
async def stream(self, prompt: Prompt, *, model: str) -> AsyncIterator[str]
async def structured(self, prompt: Prompt, schema: type[T], *, model: str) -> T
async def health(self) -> ProviderHealth
```
`structured` is the workhorse for extraction and classification. Gemini's structured-output support is why ADR-002 is low-risk.

### RelationalStorePort

```python
async def execute(self, stmt: Executable) -> Result
async def transaction(self) -> AsyncContextManager[Transaction]
```

### ObjectStorePort

```python
async def put(self, key: str, data: bytes) -> None
async def get(self, key: str) -> bytes
async def delete(self, key: str) -> None
```

### ClockPort

```python
def now(self) -> datetime          # not async, always timezone-aware UTC
```

---

## Cross-Cutting

### MigrationRunner

```python
async def apply_pending(self) -> list[AppliedMigration]
async def verify_checksums(self) -> None      # raises on drift
```

### SchemaDriftCheck

```python
async def assert_matches(self) -> None        # raises on mismatch
```

### DegradationPolicy

```python
def on_retrieval_failure(self, err: Exception) -> Degradation
def on_extraction_timeout(self, conversation_id: ConversationId) -> Degradation
def on_provider_unavailable(self, err: Exception) -> Degradation
```
Each returns a `Degradation` carrying both the fallback action and the user-facing disclosure text, so NFR-06.5's "with disclosure" clause cannot be silently dropped.

### MemoryOperationLog

```python
async def record(self, op: MemoryOperation) -> None
async def query(self, filt: OperationFilter) -> list[MemoryOperation]
```

---
---

# Addendum — Types and Methods Added by ADRs 010 to 017

These extend the definitions above. Where a field or signature differs, the addendum wins.

---

## Temporal Expression Types (ADR-010, ADR-011)

```python
class Granularity(StrEnum):
    INSTANT = "instant"
    DAY     = "day"
    WEEK    = "week"
    MONTH   = "month"
    QUARTER = "quarter"
    YEAR    = "year"
    UNKNOWN = "unknown"      # unresolvable; date MUST be null

class ResolutionMethod(StrEnum):
    ABSOLUTE       = "absolute"        # "on 3 March 2026"
    CLOCK_RELATIVE = "clock_relative"  # "three weeks ago"
    EVENT_RELATIVE = "event_relative"  # "before the wedding"
    UNRESOLVED     = "unresolved"

@dataclass(frozen=True)
class RelativeDescriptor:
    """What Gemini returns. Deliberately NOT a date."""
    direction: Literal["past", "future", "none"]
    quantity: int | None
    unit: Literal["day", "week", "month", "quarter", "year"] | None
    weekday: int | None          # for "last Tuesday"
    modifier: str | None         # "last", "next", "early", "late"

@dataclass(frozen=True)
class TemporalExpression:
    raw_phrase: str                        # never discarded (ADR-010)
    descriptor: RelativeDescriptor | None
    resolved_from: datetime | None         # None when granularity is UNKNOWN
    resolved_to: datetime | None
    granularity: Granularity
    method: ResolutionMethod
    anchor_message_id: MessageId
    anchor_zone: str                       # IANA name active at capture (ADR-011)
```

### TimeResolver

Pure, deterministic, no model calls. This is where temporal correctness is won or lost, and it is fully unit-testable.

```python
def resolve(
    self,
    descriptor: RelativeDescriptor,
    anchor: datetime,
    zone: str,
) -> tuple[datetime | None, datetime | None, Granularity]
```

Day-boundary arithmetic runs in `zone`, not UTC (ADR-011). Returns `(None, None, UNKNOWN)` rather than guessing when the descriptor is insufficient.

```python
def resolve_event_relative(
    self,
    reference: str,
    candidates: list[Event],
) -> OrderingConstraint | tuple[datetime, datetime]
```

Returns an `OrderingConstraint` when no date can be established, which is then stored as a `BEFORE` / `AFTER` relationship rather than a fabricated date.

---

## Revised Fact (ADR-011, ADR-017)

```python
@dataclass(frozen=True)
class Fact:
    id: MemoryId
    statement: str
    origin: Origin
    confidence: Confidence
    validity: TemporalValidity
    belief: BeliefWindow
    temporal_expression: TemporalExpression | None   # ADR-010
    salience: float                                  # ADR-017, 0.0 to 1.0
    subject_entity_ids: list[EntityId]
    provenance: list[ProvenanceRef]                  # now a LIST (ADR-012)
    superseded_by: MemoryId | None
```

**`provenance` becomes a list.** This is required by the corroboration rule in ADR-012 — a fact supported by three conversations must survive deletion of one. A single reference cannot express that.

---

## Entity Resolution (ADR-014)

```python
@dataclass(frozen=True)
class EntityMatch:
    entity: Entity
    score: float
    is_provisional: bool

class ResolutionOutcome(StrEnum):
    LINKED       = "linked"        # high confidence
    PROVISIONAL  = "provisional"   # ambiguous, new entity created
    CREATED      = "created"       # no match
```

```python
async def resolve_for_extraction(self, mention: str, context: str) -> ResolutionDecision
```

Never merges. On ambiguity it creates a provisional entity and emits a clarification flag. Merging remains available only through the explicit, reversible `merge` method.

```python
async def list_provisional(self) -> list[Entity]
```

Surfaces provisional entities so duplicates can be found and merged deliberately rather than accumulating unseen.

---

## Deletion (ADR-012)

Replaces the two-method `DeletionService` above.

```python
async def forget_memory(self, memory_id: MemoryId, reason: str) -> None

async def delete_source(
    self,
    source: SourceRef,
    reason: str,
) -> SourceDeletionReport
```

`delete_source` tombstones the source, then for each derived memory drops the provenance link and retracts **only if no supporting source remains**.

```python
async def erase(self, target: DeletionTarget, confirmation: str) -> EraseReport
```

Two-phase across both stores. Leaves a content-free audit stub.

```python
class BeliefChangeCause(StrEnum):
    ASSERTED        = "asserted"
    CORRECTED       = "corrected"
    SUPERSEDED      = "superseded"
    RETRACTED       = "retracted"
    SOURCE_DELETED  = "source_deleted"    # ADR-012
```

---

## Backup (ADR-013)

```python
@dataclass(frozen=True)
class BackupManifest:
    created_at: datetime
    postgres_dump_key: str
    llm_model_id: str            # required for rebuild compatibility
    embedding_model_id: str      # embeddings from different models are incomparable
    episode_count: int
    user_timezone: str
    graph_cache_key: str | None  # optional fast-restore cache, NOT a backup
```

```python
async def backup(self) -> BackupManifest
async def restore(self, manifest: BackupManifest, confirmation: str) -> RestoreReport
```

`backup` pauses `ExtractionCoordinator` for the duration so the episode log has no in-flight gaps. `restore` restores PostgreSQL then invokes `ReindexService.rebuild()`; Neo4j is never restored from a backup.

---

## Evaluation Seams (ADR-016)

No new components. Three existing capabilities are kept deliberately reachable:

```python
# 1. ClockPort already injectable — no change

# 2. Explicit-timestamp ingestion (already needed by ImportService)
async def ingest_at(self, content: str, occurred_at: datetime, zone: str) -> EpisodeId

# 3. Diagnostics persistence behind a config flag
async def persist(self, diagnostics: RetrievalDiagnostics) -> None
```

---

## Graphiti Boundary (ADR-015)

`MemoryGraphPort` gains one method, and one rule is recorded against the existing ones.

```python
async def entity_divergence(self) -> list[EntityDivergence]
```

Reports where Graphiti's internal entity consolidation disagrees with our authoritative entity records. Consumed by `ReindexService.verify`.

**Rule**: temporal and belief queries are answered from PostgreSQL-backed services (`TimelineService`, `BeliefHistoryService`). Graph edge temporal metadata is used for *candidate discovery* only, never as the answer. Graphiti is queried to find things; PostgreSQL is queried to assert what is true.
