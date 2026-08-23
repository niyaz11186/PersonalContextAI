# Requirements to Units Map

## Substitution Note

The Units Generation rules specify a `unit-of-work-story-map.md` mapping user stories to units. User Stories was permanently excluded (constraint C-1), so requirement IDs substitute for stories. This document serves the same purpose: proving every requirement has an owning unit and no requirement is orphaned.

**Owning unit** means the unit responsible for delivering the requirement completely. Where an earlier unit provides a partial or naive version, that is noted but does not transfer ownership.

---

## Functional Requirements (54)

### FR-01 Conversational Interaction

| ID | Owner | Note |
|---|---|---|
| FR-01.1 | U1 | REST API. SSE rather than WebSocket per constraint C-9 |
| FR-01.2 | U1 | SSE streaming |
| FR-01.3 | U1 | Conversation state; LangGraph thread |
| FR-01.4 | U1 | Append-only messages |
| FR-01.5 | U1 | API-first by construction |
| FR-01.6 | U7 | Text/markdown import |

### FR-02 Automatic Memory Extraction

| ID | Owner | Note |
|---|---|---|
| FR-02.1 | U2 | Naive version in U1 |
| FR-02.2 | U2 | Aggressive extraction; paired with salience (ADR-017) |
| FR-02.3 | U1 | No confirmation gate anywhere |
| FR-02.4 | U2 | `Origin` tagging |
| FR-02.5 | U2 | Provenance on every record |
| FR-02.6 | U5 | `IntentRouter` recognises the commands. Execution of "forget" lands in U6 |
| FR-02.7 | U2 | `Origin` immutable; no promote operation exists |

### FR-03 Knowledge Graph

| ID | Owner | Note |
|---|---|---|
| FR-03.1 | U2 | Entities with custom Graphiti types |
| FR-03.2 | U2 | Typed relationships |
| FR-03.3 | U2 | Entity-to-event participation |
| FR-03.4 | U6 | Explicit reversible merge. Resolution *policy* is U2 (ADR-014) |
| FR-03.5 | U2 | Attribute history with temporal validity |

### FR-04 Temporal Memory

| ID | Owner | Note |
|---|---|---|
| FR-04.1 | U2 | UTC instant + per-record zone (ADR-011) |
| FR-04.2 | U2 | `TemporalValidity` |
| FR-04.3 | U3 | Supersession |
| FR-04.4 | U3 | No destructive update path exists |
| FR-04.5 | U3 | `TimelineService.state_at` |
| FR-04.6 | U3 | `TimelineService.diff` |
| FR-04.7 | U3 | `TimelineService.reconstruct` |
| FR-04.8 | U3 | `BeliefHistoryService` |

### FR-05 Correction and Deletion

| ID | Owner | Note |
|---|---|---|
| FR-05.1 | U5 | `CorrectionWorkflow`. `MemoryService.correct` is U3 |
| FR-05.2 | U3 | Versions preserved; original retained in audit trail |
| FR-05.3 | U6 | `forget_memory` |
| FR-05.4 | U6 | Logical deletion; source deletion retracts with corroboration rule (ADR-012) |
| FR-05.5 | U3 | `believed_at` — the second time axis |
| FR-05.6 | U3 | `ConflictDetectionService` surfaces, never resolves |

### FR-06 Context Retrieval

| ID | Owner | Note |
|---|---|---|
| FR-06.1 | U1 | Retrieve-before-respond established in the skeleton |
| FR-06.2 | U4 | Five strategies, concurrent, fused |
| FR-06.3 | U4 | `RetrievalBudgetGovernor` stop condition |
| FR-06.4 | U4 | `GeminiRerankerClient` |
| FR-06.5 | U4 | Context ceiling |
| FR-06.6 | U4 | Budget permits 30s |

### FR-07 Context Construction

| ID | Owner | Note |
|---|---|---|
| FR-07.1 | U4 | Naive version in U1 |
| FR-07.2 | U4 | Four-way structural split |
| FR-07.3 | U4 | Package composition |
| FR-07.4 | U4 | `render` separated from `assemble`; source excerpts |

### FR-08 Agent Orchestration

| ID | Owner | Note |
|---|---|---|
| FR-08.1 | U1 | Multi-node LangGraph workflow |
| FR-08.2 | U5 | All five workflows |
| FR-08.3 | U5 | Conditional edges + checkpointer |
| FR-08.4 | **None** | Process requirement, already satisfied by ADR-006 during Application Design. No construction work |

### FR-09 Memory Inspection API

| ID | Owner | Note |
|---|---|---|
| FR-09.1 | U6 | Search and browse memories |
| FR-09.2 | U6 | Entity details and relationships |
| FR-09.3 | U6 | Provenance chains |
| FR-09.4 | U6 | Timeline queries |
| FR-09.5 | **None** | Scope statement — no UI in MVP. Satisfied by omission |

### FR-10 Export and Backup

| ID | Owner | Note |
|---|---|---|
| FR-10.1 | U7 | Full export |
| FR-10.2 | U7 | Backup/restore, PostgreSQL-only per ADR-013 |
| FR-10.3 | U7 | JSON/markdown |

**Coverage: 52 of 54 assigned to units. 2 (FR-08.4, FR-09.5) satisfied without construction work, justified above.**

---

## Non-Functional Requirements (36)

### NFR-01 Privacy

| ID | Owner | Note |
|---|---|---|
| NFR-01.1 | U1 | Local Compose; only Gemini egress |
| NFR-01.2 | U7 | Written egress inventory — owed since Application Design |
| NFR-01.3 | **Infrastructure Design** | Encryption-at-rest mechanism deferred |
| NFR-01.4 | U1 | TLS via Google GenAI SDK |
| NFR-01.5 | U1 | Env-based config, fail-fast |
| NFR-01.6 | U6 | `erase` with confirmation |

### NFR-02 Performance

| ID | Owner | Note |
|---|---|---|
| NFR-02.1 | U4 | Budget governor |
| NFR-02.2 | U5 | Per-conversation barrier, no global lock |
| NFR-02.3 | U5 | Background extraction. **U1 knowingly violates this**; U5 retires the exception |
| NFR-02.4 | **Build and Test** | Scale over months — verified, not assumed |

### NFR-03 Deployment

| ID | Owner | Note |
|---|---|---|
| NFR-03.1 | U1 | Docker Compose |
| NFR-03.2 | U1 | Neo4j CE, PostgreSQL, filesystem — all free |
| NFR-03.3 | U1 | Gemini is the only paid dependency |
| NFR-03.4 | U1 | Single `docker-compose up` |
| NFR-03.5 | U7 | Cloud-deployability kept open, not exercised |

### NFR-04 Provider Independence

| ID | Owner | Note |
|---|---|---|
| NFR-04.1 | U1 | `LLMProviderPort` |
| NFR-04.2 | U1 | Model IDs are configuration |
| NFR-04.3 | U1 | Gemini primary |
| NFR-04.4 | U1 | Per-task model config |
| NFR-04.5 | U1 | Gemini-only per corrected requirement text |

### NFR-05 Architecture

| ID | Owner | Note |
|---|---|---|
| NFR-05.1 | U1 | Modular monolith |
| NFR-05.2 | U1 | No broker, Redis, K8s, or MinIO |
| NFR-05.3 | U1 | Ports isolate replaceable dependencies |
| NFR-05.4 | **Cross-cutting** | Correctness over features — a principle governing every unit, not a deliverable |
| NFR-05.5 | U3 | Data integrity over summarisation |
| NFR-05.6 | U1 | Observability scaffolding; diagnostics deepen in U4 |

### NFR-06 Reliability

| ID | Owner | Note |
|---|---|---|
| NFR-06.1 | U5 | `DegradationPolicy`. Adapter retry/backoff is U1 |
| NFR-06.2 | U1 | PostgreSQL commit precedes model calls |
| NFR-06.3 | U3 | Transaction boundaries |
| NFR-06.4 | U1 | Structured error logging |
| NFR-06.5 | U5 | Degradation carries fallback **and** disclosure |
| NFR-06.6 | U7 | Full per-dependency health. Basic health in U1 |

### NFR-07 Maintainability

| ID | Owner | Note |
|---|---|---|
| NFR-07.1 | **Build and Test** | Linter configuration |
| NFR-07.2 | **Build and Test** | Coverage targets |
| NFR-07.3 | U1 | Python type hints. TypeScript out of scope — no frontend |
| NFR-07.4 | U1 | Layer-first tree + import rules |

**Coverage: 31 of 36 assigned to units. 3 to Build and Test, 1 to Infrastructure Design, 1 cross-cutting principle. All justified.**

---

## Requirement Load by Unit

| Unit | FRs | NFRs | Total |
|---|---|---|---|
| U1 Walking Skeleton | 9 | 18 | 27 |
| U2 Extraction Depth | 11 | 0 | 11 |
| U3 Temporal Integrity | 10 | 3 | 13 |
| U4 Retrieval Depth | 8 | 2 | 10 |
| U5 Orchestration Depth | 4 | 4 | 8 |
| U6 Management and Inspection | 8 | 2 | 10 |
| U7 Lifecycle and Hardening | 4 | 3 | 7 |
| Not unit-assigned | 2 | 5 | 7 |
| **Total** | **56*** | **37*** | — |

*Counts exceed 54 and 36 because FR-02.6, FR-03.4, and FR-05.1 have split delivery noted across two units; each still has exactly one **owner**. The owner assignment is what matters for construction sequencing.

### Observation on the distribution

Unit 1 carries 27 requirements — far more than any other unit. That is expected and correct for a walking skeleton: it establishes every architectural NFR (deployment, provider abstraction, layering, monolith structure) at minimal functional depth. The functional requirements it owns are thin slices; the non-functional ones it owns are structural and genuinely complete.

The concentration is worth watching during construction. If Unit 1 grows beyond a few days of work, the skeleton is being over-built and functional depth should be pushed to Units 2 onward.

---

## Validation

| Check | Result |
|---|---|
| Every FR has an owner or documented exemption | Pass — 52 owned, 2 exempt with justification |
| Every NFR has an owner or documented exemption | Pass — 31 owned, 5 exempt with justification |
| No requirement assigned to two owners | Pass — split delivery noted, ownership singular |
| No circular unit dependencies | Pass — matrix is lower triangular |
| Every unit has a stated completion criterion | Pass — see `unit-of-work.md` |
| Knowing NFR violations recorded | Pass — U1 violates NFR-02.3, retired in U5 |
