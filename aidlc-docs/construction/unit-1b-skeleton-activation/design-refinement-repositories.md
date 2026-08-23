# Design Refinement — Repository Ports

**Raised**: 2026-08-22, during Unit 1b implementation
**Affects**: Application Design (`components.md`, `component-methods.md`, `component-dependency.md`)

## The contradiction

Two pieces of the approved Application Design cannot both hold.

`component-dependency.md` boundary rule 3 states:

> No `sqlalchemy` import in L3 — detects SQL leaking into domain logic

But `RelationalStorePort` was specified as a generic primitive:

```python
async def execute(self, stmt: Executable) -> Result
def transaction(self) -> AsyncContextManager[Transaction]
```

A domain service holding only that port has no way to express "insert a message"
without constructing a SQLAlchemy statement — which is exactly what rule 3
forbids. The design was internally inconsistent, and it only became visible when
`ConversationService` needed to be written.

## Resolution

Introduce **repository ports** between domain services and storage.

| Layer | Depends on | Speaks |
|---|---|---|
| L3 services | repository ports | domain types only |
| L5 repository adapters | `RelationalStorePort` | SQLAlchemy Core statements |
| L5 `PostgresStoreAdapter` | asyncpg | connections and transactions |

`RelationalStorePort` is retained but its consumer changes: it is now used *by
adapters*, not by services. It remains the transaction primitive, which is
necessary because a memory commit must write memory rows, the operation log, and
belief records atomically across several repositories.

## Ports added

| Port | Owns |
|---|---|
| `ConversationRepositoryPort` | conversations, messages |
| `EpisodeRepositoryPort` | episodes, including the replay watermark |

Later units add `MemoryRepositoryPort`, `BeliefRepositoryPort`,
`OperationLogRepositoryPort` on the same pattern.

## Why this is the right resolution rather than relaxing rule 3

Rule 3 exists so that swapping the storage layer does not require touching domain
logic. Allowing services to build SQLAlchemy statements would spread PostgreSQL
assumptions through L3 and quietly forfeit that property — the same class of
coupling that boundary rules 1 and 2 prevent for Graphiti and LangGraph.

It also preserves testability. Domain services can now be tested against
in-memory repository fakes with no SQL and no database, which is what allows this
work to proceed while the container runtime is unavailable.

## Cost

More ports, and a small amount of mapping code in each repository adapter. That
mapping is where row-to-domain translation belongs anyway; the alternative was
scattering it through service methods.

## Traceability impact

No requirement changes. `NFR-05.3` (evolutionary architecture) and `NFR-07.4`
(separation of concerns) are better served than before.
