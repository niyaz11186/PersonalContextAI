# Application Design Plan

## Context

Architecture decisions are recorded in `aidlc-docs/inception/application-design/architecture-decisions.md`. Five of seven ADRs are settled from your constraints and from research evidence. Two need your input, plus two smaller implementation choices.

I have deliberately kept this to **4 questions**. Everything else I could decide from your existing answers or from evidence.

Please answer by filling in the letter after each `[Answer]:` tag.

---

## Question 1
**This is the important one.** Should PostgreSQL be the system of record, with Neo4j/Graphiti as a rebuildable projection? (Full reasoning in ADR-005.)

Your specification said the domain model must not be "dictated by a generic memory framework" and that raw source material must remain available. That points toward keeping your own copy of conversations independent of Graphiti — so the graph can always be rebuilt from source if Graphiti changes, breaks, or you want to re-extract with a better model.

A) Yes — PostgreSQL is the system of record. Conversations, messages, and an extraction log live there. Neo4j is a derived projection that can be rebuilt by replaying episodes. Rebuild/reindex is a first-class feature. (Recommended: protects your history from any memory-framework decision, at the cost of some sync logic.)

B) No — let Graphiti/Neo4j be the sole store. PostgreSQL only handles sessions and config. Simpler and faster to build, but your history depends entirely on Graphiti's correctness and schema stability, and re-extraction becomes impossible.

C) Hybrid — PostgreSQL stores conversations for durability, but no extraction log or belief-history tables; rely on Graphiti for all derived memory and accept that re-extraction produces different results.

X) Other (please describe after [Answer]: tag below)

[Answer]:A)

---

## Question 2
Should memory extraction block the user's response, or run in the background?

You said accuracy over speed and accepted 20-30s latency (Q4), but also that extraction should not block the response (NFR-02.3). Those pull in opposite directions, so this needs an explicit call.

The real trade-off: if extraction is backgrounded, a fact you state in one message may not be retrievable in your very next message. If it blocks, every message pays the extraction cost.

A) Blocking — extract before responding. Guarantees the memory is queryable immediately, including in the next message. Every turn pays full latency.

B) Background — respond fast using existing memory, extract afterward. Fast responses, but a just-stated fact may be briefly unavailable.

C) Hybrid — respond immediately, extract in background, but block if the user's next message arrives before extraction finishes (a write barrier per conversation). Best of both, moderate complexity. (Recommended.)

D) Background by default, with an explicit "remember this" command forcing blocking extraction.

X) Other (please describe after [Answer]: tag below)

[Answer]:C)

---

## Question 3
Confirm LangGraph for orchestration? (Reasoning and the counter-argument in ADR-006.)

A) Yes — use LangGraph, confined to the orchestration layer, with PostgreSQL checkpointing. Gets durable execution and resumable interrupts for free. (Recommended.)

B) No — plain async Python with explicit state objects. Fewer dependencies, easier to debug, but checkpointing and resumable clarification pauses must be hand-built.

C) Start with plain async Python; introduce LangGraph only if the workflows prove to need checkpointing.

X) Other (please describe after [Answer]: tag below)

[Answer]:A)

---

## Question 4
How should PostgreSQL be accessed from Python, given raw SQL migrations (ADR-004)?

A) `asyncpg` directly with hand-written SQL queries. Fastest, no ORM overhead, fully consistent with the raw-SQL decision. More boilerplate for row-to-object mapping.

B) SQLAlchemy Core (not ORM) for query building, with raw SQL migrations. Composable queries, still no ORM magic, schema stays in .sql files. (Recommended: keeps your raw-SQL constraint while avoiding string-concatenation query building.)

C) SQLAlchemy ORM for queries, raw SQL for migrations. Convenient object mapping, but the model definitions and .sql files can drift out of sync.

X) Other (please describe after [Answer]: tag below)

[Answer]:B) SQLAlchemy Core (not ORM) for query building, with raw SQL migrations.

---

## Design Artifact Generation Steps (execute after answers received)

- [x] Step 1: Finalize ADR-005 and ADR-006 based on answers
- [x] Step 2: Generate `components.md` — component definitions, responsibilities, interfaces
- [x] Step 3: Generate `component-methods.md` — method signatures and input/output types
- [x] Step 4: Generate `services.md` — service definitions and orchestration patterns
- [x] Step 5: Generate `component-dependency.md` — dependency matrix, communication patterns, data flow
- [x] Step 6: Generate consolidated `application-design.md`
- [x] Step 7: Validate design completeness against all 41 FRs and 28 NFRs
