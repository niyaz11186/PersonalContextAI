# Unit of Work Plan

## Context

Application Design is approved with 17 ADRs. The layer dependency graph already constrains most of the decomposition. What remains genuinely open is the **shape** of the work — the order in which capability is built, not which components exist.

**4 questions.** Question 1 is the consequential one.

---

## Question Category Applicability

The decomposition rules require evaluating six categories. Three do not apply here, with justification:

| Category | Applies | Justification |
|---|---|---|
| Story Grouping | **No** | User Stories stage was permanently excluded (constraint C-1). Requirements IDs (FR/NFR) substitute for stories in the unit mapping artifact. |
| Dependencies | **Yes** | Addressed by Q1 and Q3 |
| Team Alignment | **No** | Single developer, single reviewer. No ownership boundaries to negotiate. |
| Technical Considerations | **Yes** | Addressed by Q3 |
| Business Domain | **No** | One bounded context: personal context management. No sub-domain boundaries to draw. |
| Code Organization | **Yes** | Greenfield. Addressed by Q4 |

---

## Question 1
**The consequential one.** What shape should the build take?

The seven units proposed in Application Design are **horizontal** — one layer at a time, Foundation through Infrastructure. That gives clean dependencies, but nothing works end to end until Unit 6. You would have five units of code you cannot actually interact with.

A) **Horizontal layers** — build each layer completely before the next. Clean dependency order, no rework. But no working system until Unit 6, and integration problems with Graphiti/Gemini/Neo4j surface late, when they are expensive.

B) **Walking skeleton first, then deepen** — Unit 1 is a thin vertical slice through every layer: real Postgres, real Neo4j, real Gemini, but naive implementations. A message goes in, gets stored, gets trivially retrieved, and a response comes out. Then each subsequent unit deepens one capability. Integration risk is front-loaded to day one and you have something to interact with immediately. Cost: the naive implementations get replaced, though with ports already in place "replaced" mostly means filling in an adapter. *(Recommended — reasoning below.)*

C) **Hybrid: foundation, then skeleton, then deepen** — Unit 1 is config, migrations, and database connectivity only. Unit 2 is the walking skeleton. Then deepen. Slightly more conservative than B, one extra step before anything is interactive.

X) Other (please describe after [Answer]: tag below)

[Answer]:B)

### Why B is recommended

Three reasons specific to this project rather than general preference:

**The riskiest thing in the stack is whether Graphiti, Gemini, and Neo4j actually work together as documented.** That is the top entry in your risk register. Option A discovers this in Unit 3 at the earliest, after two units of work are already committed to assumptions. Option B discovers it on day one.

**This product's value is subjective and has to be felt.** The core hypothesis is about whether the system *usefully* remembers. You cannot evaluate that by reading code — you need to talk to it and be surprised by what it recalls. Being able to do that from Unit 1 changes how well you can steer the remaining work.

**You are working alone.** Horizontal layering pays off when separate people build separate layers in parallel. With one developer it mostly defers feedback.

The honest cost: some throwaway work. A naive retrieval implementation gets discarded when the real hybrid retrieval lands. Given the ports are already designed, that discard is contained.

---

## Question 2
After the skeleton works, which half of the hypothesis do you want to deepen first?

The core hypothesis has two halves, and they fail differently. You cannot meaningfully work both at once, because improving one changes how you evaluate the other.

A) **Extraction first** — make the system genuinely good at noticing and structuring what matters from conversation (facts, events, entities, temporal expressions, salience). Retrieval stays naive meanwhile. Rationale: you cannot retrieve what was never captured well, and bad extraction is invisible until much later.

B) **Retrieval first** — make hybrid retrieval, ranking, and context assembly genuinely good, working over whatever the naive extraction produced. Rationale: retrieval failures are immediately visible and gratifying to fix, and it tells you what extraction actually needs to produce.

C) **Alternate in short passes** — deepen extraction a little, then retrieval, repeatedly. More context-switching, but keeps both halves roughly in balance.

X) Other (please describe after [Answer]: tag below)

[Answer]:A)

**My lean is A**, narrowly. Extraction mistakes are permanent — a fact captured with the wrong date or attributed to the wrong person poisons the graph, and you may not notice for months. Retrieval mistakes are recoverable at any time because the data is still there and you can just re-run a better query. Fixing the irreversible thing first is the safer order.

---

## Question 3
How much infrastructure should exist in the first unit?

Whatever the answer to Q1, you need Postgres and Neo4j actually running before you can build anything. The question is how much of Unit 7 moves to the front.

A) **Minimal dev environment early** — just enough Docker Compose to get Postgres and Neo4j 5.26+ running with persistent volumes. Health checks, backup/restore, reindex, and export stay in the final unit. *(Recommended.)*

B) **Full infrastructure early** — Compose, health checks, backup, restore, reindex, and export all up front. Nothing infrastructural left at the end, but it delays functional work and some of it cannot be meaningfully tested yet (reindex needs episodes to replay).

C) **No containers initially** — install Postgres and Neo4j directly on the machine, add Docker Compose later. Faster to start, but diverges from NFR-03.1 and you would eventually debug two environments.

X) Other (please describe after [Answer]: tag below)

[Answer]:A)

---

## Question 4
Directory structure for the codebase?

Modular monolith, single deployable application, Python backend, no frontend in MVP.

A) **Layer-first** — top-level packages mirror the six layers: `api/`, `orchestration/`, `services/`, `ports/`, `adapters/`, `infrastructure/`. Makes the layering rules visually obvious and boundary violations easy to spot in review — which matters more now that the CI linter was dropped.

B) **Feature-first** — top-level packages per capability: `conversation/`, `memory/`, `retrieval/`, `entities/`, each containing its own layers internally. Related code sits together; layer boundaries are less visible.

C) **Hybrid** — `api/` and `adapters/` at top level (the edges), with domain logic grouped by feature inside `core/`.

X) Other (please describe after [Answer]: tag below)

[Answer]:A)

**My lean is A.** You dropped the import linter, so boundary rules 1, 2, and 7 now depend on you noticing violations during review. A layer-first tree makes a violation visible as a wrong import path rather than something you have to reason about.

---

## Generation Steps (execute after answers and approval)

- [x] Step 1: Finalize unit decomposition based on answers to Q1–Q4
- [x] Step 2: Generate `unit-of-work.md` — unit definitions, responsibilities, scope boundaries, and code organization strategy
- [x] Step 3: Generate `unit-of-work-dependency.md` — dependency matrix and build sequence
- [x] Step 4: Generate `unit-of-work-requirements-map.md` — map all 54 FRs and 36 NFRs to units (substitutes for the story map, per constraint C-1)
- [x] Step 5: Validate that every unit has a clear completion criterion
- [x] Step 6: Validate that every requirement is assigned to exactly one owning unit
- [x] Step 7: Confirm no circular dependencies between units
