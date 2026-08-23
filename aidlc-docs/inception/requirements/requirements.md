# Requirements Document — Personal Context AI Assistant

## Intent Analysis

| Attribute | Value |
|-----------|-------|
| **User Request** | Build a private, persistent personal-context AI assistant with longitudinal memory |
| **Request Type** | New Project (Greenfield) |
| **Scope** | System-wide — full-stack application with AI orchestration, temporal knowledge graph, hybrid retrieval, and evaluation |
| **Complexity** | Complex — novel memory primitives, temporal reasoning, multi-agent workflows, provider abstraction |
| **Depth** | Comprehensive |

---

## 1. Product Summary

A single-user, self-hosted AI assistant that maintains a structured, temporal knowledge graph of the user's personal context across conversations spanning weeks, months, and years. The system extracts and organizes memories automatically, supports correction and deletion, preserves provenance, and retrieves relevant historical context when answering future questions.

**Core Hypothesis**: A user can tell the system important information today and, weeks or months later, the system can retrieve and correctly use that information when it is relevant to a new question.

---

## 2. User Profile

- **Primary User**: Single user (the developer/owner)
- **Authentication**: Not required for MVP (local access only)
- **Multi-tenancy**: Not required; single-user, single-context
- **Future consideration**: Potential for small trusted group access post-MVP

---

## 3. Functional Requirements

### FR-01: Conversational Interaction (API-First)

| ID | Requirement |
|----|-------------|
| FR-01.1 | The system SHALL expose a REST/WebSocket API for sending messages and receiving responses |
| FR-01.2 | The system SHALL support multi-turn conversations with streaming responses |
| FR-01.3 | The system SHALL maintain conversation state within a session |
| FR-01.4 | The system SHALL persist all conversations as immutable source material |
| FR-01.5 | The API SHALL be designed for future frontend integration (web UI to be built later) |
| FR-01.6 | The system SHALL support basic text/markdown import of external documents into the memory system |

### FR-02: Automatic Memory Extraction

| ID | Requirement |
|----|-------------|
| FR-02.1 | The system SHALL automatically extract candidate facts, events, entities, and relationships from every conversation |
| FR-02.2 | The system SHALL extract aggressively to avoid missing important information |
| FR-02.3 | The system SHALL NOT require user confirmation for memory extraction (fully automatic) |
| FR-02.4 | The system SHALL distinguish between user-provided facts and AI-generated interpretations |
| FR-02.5 | The system SHALL tag all extracted memories with source provenance (conversation ID, message ID, timestamp) |
| FR-02.6 | The system SHALL support explicit user commands: "remember this", "forget this", "correct this" |
| FR-02.7 | AI interpretations SHALL never silently become user-provided facts |

### FR-03: Knowledge Graph — Entities and Relationships

| ID | Requirement |
|----|-------------|
| FR-03.1 | The system SHALL maintain a knowledge graph of entities (people, organizations, places, projects) |
| FR-03.2 | The system SHALL track typed relationships between entities (family, colleague, friend, etc.) |
| FR-03.3 | The system SHALL track relationships between entities and events (person involved in incident, etc.) |
| FR-03.4 | The system SHALL support entity merging when the same entity is referenced differently across conversations |
| FR-03.5 | The system SHALL track entity attributes with temporal validity (e.g., "Person A lives in X" valid from date Y) |

### FR-04: Temporal Memory

| ID | Requirement |
|----|-------------|
| FR-04.1 | All memories SHALL have timestamps (creation time, event time where applicable) |
| FR-04.2 | Facts SHALL support valid-from / valid-to periods |
| FR-04.3 | The system SHALL track supersession (fact B supersedes fact A) |
| FR-04.4 | The system SHALL preserve historical states — newer information does NOT delete older information |
| FR-04.5 | The system SHALL support "what was true at time T?" queries |
| FR-04.6 | The system SHALL support "what changed between T1 and T2?" queries |
| FR-04.7 | The system SHALL support chronological timeline reconstruction for entities and events |
| FR-04.8 | The system SHALL track the full belief history — what was believed at each point in time, including corrections |

### FR-05: Memory Correction and Deletion

| ID | Requirement |
|----|-------------|
| FR-05.1 | The system SHALL support user corrections ("that's not what I said", "this changed") |
| FR-05.2 | Corrections SHALL create new versions; the original SHALL be preserved in an audit trail |
| FR-05.3 | The system SHALL support memory deletion ("forget this") |
| FR-05.4 | Deletion SHALL be logical (marked as deleted with timestamp) rather than physical, preserving audit trail |
| FR-05.5 | The system SHALL support "what did I think was true at time T?" (full temporal correction) |
| FR-05.6 | The system SHALL handle contradictions by surfacing them rather than silently choosing a version |

### FR-06: Context Retrieval

| ID | Requirement |
|----|-------------|
| FR-06.1 | Before generating a response, the system SHALL retrieve relevant historical context |
| FR-06.2 | Retrieval SHALL use a hybrid approach: semantic, keyword/full-text, structured filtering, entity-based, temporal filtering, graph traversal, and relationship-based retrieval |
| FR-06.3 | The system SHALL retrieve the smallest useful set of context — not everything that looks similar |
| FR-06.4 | The system SHALL apply relevance ranking/reranking to retrieved context |
| FR-06.5 | The system SHALL NOT inject large quantities of irrelevant historical information into the LLM context |
| FR-06.6 | Accuracy is prioritized over speed — latency up to 20-30 seconds is acceptable for thorough retrieval |

### FR-07: Context Construction

| ID | Requirement |
|----|-------------|
| FR-07.1 | The system SHALL construct an explicit context package before generating the final response |
| FR-07.2 | The context package SHALL distinguish: user-stated facts, system-derived information, current AI beliefs, and uncertain information |
| FR-07.3 | The context package MAY include: relevant facts, events, entities, relationships, timeline info, previous conversations, AI interpretations (clearly labeled) |
| FR-07.4 | The context construction SHALL be designed to reduce hallucinated historical context |

### FR-08: Agent Orchestration

| ID | Requirement |
|----|-------------|
| FR-08.1 | The system SHALL support multi-step reasoning workflows (not single LLM calls) |
| FR-08.2 | The system SHALL implement at minimum these workflows: normal conversation, new information extraction, user correction, historical analysis, ambiguous/uncertain memory handling |
| FR-08.3 | Workflows SHALL be conditional and stateful |
| FR-08.4 | The orchestration layer SHALL be evaluated — LangGraph is a candidate but alternatives should be assessed |

### FR-09: Memory Inspection API

| ID | Requirement |
|----|-------------|
| FR-09.1 | The system SHALL expose API endpoints for browsing, searching, and querying stored memories |
| FR-09.2 | The system SHALL expose API endpoints for viewing entity details and relationships |
| FR-09.3 | The system SHALL expose API endpoints for viewing memory provenance chains |
| FR-09.4 | The system SHALL expose API endpoints for viewing timeline/chronological data |
| FR-09.5 | A UI for memory inspection is deferred to post-MVP; API endpoints are sufficient |

### FR-10: Data Export and Backup

| ID | Requirement |
|----|-------------|
| FR-10.1 | The system SHALL support full data export (conversations, memories, knowledge graph) |
| FR-10.2 | The system SHALL support backup and restore operations |
| FR-10.3 | Export format SHALL be human-readable where practical (JSON, markdown) |

---

## 4. Non-Functional Requirements

### NFR-01: Privacy and Data Ownership

| ID | Requirement |
|----|-------------|
| NFR-01.1 | All data SHALL remain on the user's local machine (except LLM API calls) |
| NFR-01.2 | The system SHALL clearly identify which data is sent to external LLM/embedding providers |
| NFR-01.3 | The system SHALL support encryption at rest for stored personal data |
| NFR-01.4 | The system SHALL use HTTPS/TLS for all external API communications |
| NFR-01.5 | Credentials and API keys SHALL be stored securely (environment variables, not in code) |
| NFR-01.6 | The system SHALL support complete data deletion upon user request |

### NFR-02: Performance

| ID | Requirement |
|----|-------------|
| NFR-02.1 | Response latency up to 30 seconds is acceptable when complex retrieval is involved |
| NFR-02.2 | The system SHALL handle heavy usage: multiple daily sessions, 50+ messages per day |
| NFR-02.3 | Memory extraction SHALL NOT block the user from receiving a conversational response |
| NFR-02.4 | The system SHALL remain performant as the knowledge graph grows over months/years of use |

### NFR-03: Deployment and Operations

| ID | Requirement |
|----|-------------|
| NFR-03.1 | MVP deployment SHALL be entirely local via Docker Compose |
| NFR-03.2 | All infrastructure components (databases, graph store) SHALL be free/open-source |
| NFR-03.3 | The only paid services SHALL be LLM API providers |
| NFR-03.4 | The system SHALL be startable with a single `docker-compose up` command |
| NFR-03.5 | Post-MVP: the system SHALL be deployable to a personal cloud instance (design for this, don't block it) |

### NFR-04: LLM Provider Independence

| ID | Requirement |
|----|-------------|
| NFR-04.1 | The system SHALL abstract LLM provider access behind a common interface |
| NFR-04.2 | The system SHALL support switching providers without code changes (configuration-driven) |
| NFR-04.3 | Initial provider: Gemini preferred for cost, with ability to switch to others for quality |
| NFR-04.4 | The system SHALL support using different providers for different tasks (reasoning, embeddings, classification) |
| NFR-04.5 | MVP supports Google Gemini only (constraint C-2 excludes OpenAI). The port interface SHALL remain provider-neutral so additional adapters can be added later without changing domain code. Revised 2026-08-11. |

### NFR-05: Architecture

| ID | Requirement |
|----|-------------|
| NFR-05.1 | The system SHALL use a modular monolith architecture (no premature microservices) |
| NFR-05.2 | The system SHALL NOT introduce infrastructure beyond what requirements justify (no Redis, Kafka, Kubernetes for MVP) |
| NFR-05.3 | The system SHALL be designed for evolutionary architecture — components replaceable without full rewrite |
| NFR-05.4 | The system SHALL prioritize correctness over feature count |
| NFR-05.5 | The system SHALL prioritize data integrity over aggressive summarization |
| NFR-05.6 | The system SHALL be testable and observable |

### NFR-06: Reliability (Resiliency Baseline — Enabled)

| ID | Requirement |
|----|-------------|
| NFR-06.1 | The system SHALL handle LLM API failures gracefully (retry with backoff, then graceful degradation with disclosure). No fallback provider in MVP — single provider by decision. Revised 2026-08-11. |
| NFR-06.2 | The system SHALL NOT lose conversation or memory data due to process crashes |
| NFR-06.3 | Database operations SHALL be transactional where data integrity requires it |
| NFR-06.4 | The system SHALL log errors and provide meaningful error messages |
| NFR-06.5 | The system SHALL support graceful degradation (e.g., respond without memory if retrieval fails, with disclosure) |
| NFR-06.6 | The system SHALL implement health checks for all infrastructure components |

### NFR-07: Maintainability and Code Quality

| ID | Requirement |
|----|-------------|
| NFR-07.1 | Code SHALL follow consistent style and conventions (enforced by linters) |
| NFR-07.2 | The system SHALL have meaningful test coverage for critical paths (memory extraction, retrieval, correction) |
| NFR-07.3 | The system SHALL use typed interfaces (TypeScript for frontend concerns, Python type hints for backend) |
| NFR-07.4 | The system SHALL maintain clear separation of concerns between orchestration, memory, retrieval, and API layers |

---

## 5. Technical Context

### 5.1 Technology Stack (Evaluated from User Preferences + Constraints)

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| **Backend** | Python + FastAPI | User preference; strong AI/ML ecosystem; async-native |
| **Agent Orchestration** | LangGraph (to evaluate) | Candidate for stateful conditional workflows; alternatives to be assessed |
| **Long-Term Memory** | Graphiti (to evaluate) | Temporal knowledge graph; purpose-built for the problem; alternatives (Mem0, Letta) to be assessed |
| **Graph Database** | Neo4j Community Edition (Docker) | Free, self-hosted, Graphiti-compatible, accuracy-optimized |
| **Application Database** | PostgreSQL (Docker) | Free, robust, handles conversations/sessions/audit |
| **Embeddings** | Gemini (`GeminiEmbedder`) | Constraint C-2. Abstracted behind interface for future swap |
| **Reranking** | Gemini (`GeminiRerankerClient`) | Constraint C-2. Graphiti cross-encoder role |
| **Deployment** | Docker Compose (local) | Single-command startup, all services containerized |
| **Future Frontend** | Next.js / React / TypeScript | Deferred to post-MVP; API-first design enables this |

### 5.2 Architecture Decision Records

All resolved. See `aidlc-docs/inception/application-design/architecture-decisions.md` for 17 ADRs with evidence and rejected alternatives.

| Original open question | Resolution |
|---|---|
| Orchestration: LangGraph vs. alternatives | ADR-006 — LangGraph, confined to orchestration layer |
| Memory framework: Graphiti vs. Mem0 vs. Letta vs. custom | ADR-001 — Graphiti; Mem0 and Letta rejected with reasons |
| Graph DB: Neo4j vs. PostgreSQL-only | ADR-003 — Neo4j 5.26+ CE |
| Retrieval strategy | ADR-005, and services.md read path |
| Provider abstraction: LiteLLM vs. custom | ADR-007 — thin custom port, no LiteLLM |

### 5.3 Integration Points

| Integration | Direction | Data Exchanged |
|-------------|-----------|---------------|
| Google Gemini (LLM) | Outbound | Conversation context, prompts, structured extraction queries |
| Google Gemini (embeddings) | Outbound | Text chunks for embedding generation |
| Google Gemini (reranking) | Outbound | Query plus candidate passages for cross-encoding |
| Neo4j (local Docker) | Internal | Entity/relationship graph operations |
| PostgreSQL (local Docker) | Internal | Conversations, messages, audit trail, configuration |

---

## 6. MVP Scope Boundary

### In Scope (MVP)

1. API-based conversational interaction (no UI)
2. Automatic memory extraction from all conversations
3. Persistent conversation storage with immutability
4. Knowledge graph: entities, relationships, temporal facts
5. Temporal memory: valid-from/to, supersession, belief history
6. Hybrid context retrieval (semantic + structured + graph)
7. Context construction with provenance and type distinctions
8. Multi-step agent workflows (conversation, extraction, correction, historical queries)
9. Memory correction with full temporal versioning
10. Memory deletion (logical)
11. Provenance tracking to source conversations
12. Memory inspection API endpoints
13. Basic text/markdown import
14. Data export and backup
15. LLM provider abstraction (multi-provider support)
16. Docker Compose local deployment
17. Evaluation: deferred decision (scripted scenarios likely)

### Out of Scope (MVP)

- Web/mobile UI (API-only for now)
- Authentication/authorization
- Multi-user/multi-tenant
- SaaS billing
- Mobile applications
- Autonomous actions
- Complex notification systems
- Kubernetes / cloud-native deployment
- Social features
- Third-party integrations beyond LLM providers
- Property-based testing framework

---

## 7. Extension Configuration

| Extension | Enabled | Rationale |
|-----------|---------|-----------|
| Security Baseline | No | Experimental/personal project; local-only deployment |
| Resiliency Baseline | Yes | User wants standards and good practices even for experimental work |
| Property-Based Testing | No | User unfamiliar; deferred to avoid blocking progress |

---

## 8. Key Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Memory extraction missing important information | Core hypothesis failure | Aggressive extraction + user-accessible correction; evaluation scenarios |
| Knowledge graph becomes inconsistent over time | Incorrect responses | Temporal versioning, provenance, contradiction detection |
| Retrieval returning irrelevant context | Poor response quality | Hybrid retrieval with reranking; iterative tuning |
| LLM hallucinating historical context | Trust erosion | Explicit context construction; fact vs. interpretation separation |
| Neo4j + PostgreSQL operational complexity | Development friction | Docker Compose automation; consider PostgreSQL-only if Neo4j adds too much burden |
| Graphiti/LangGraph immaturity or API instability | Rework | Evaluate alternatives early; keep abstractions thin; be prepared to swap |
| Data loss from local-only storage | Irreversible loss | Backup/export from day one; documented recovery process |

---

## 9. Success Criteria

The MVP is successful if:

1. A user can tell the system an important fact today
2. Several unrelated conversations occur over simulated weeks
3. The user asks a question related to that original fact
4. The system retrieves the correct historical context without the user repeating it
5. The system correctly distinguishes what the user said vs. what it inferred
6. The system can show provenance (where it learned the information)
7. The user can correct a memory and the system respects the correction in future responses
8. The system handles temporal changes (e.g., "person moved from X to Y") correctly
9. The system runs entirely locally with a single `docker-compose up` command
