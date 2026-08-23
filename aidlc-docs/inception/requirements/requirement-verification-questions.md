# Requirements Verification Questions

Your specification is remarkably thorough. The following questions target areas where the answers will materially affect architecture decisions, MVP boundary, and implementation strategy. Please answer each question by filling in the letter choice after the `[Answer]:` tag.

---

## Question 1
Who is the intended user for the MVP?

A) Only you (single-user, self-hosted, no authentication needed beyond local access)

B) You primarily, but potentially shared with a small trusted group (e.g., partner/family) in future

C) A small number of individual users, each with their own isolated context (multi-tenant from the start)

X) Other (please describe after [Answer]: tag below)

[Answer]:A) 

---

## Question 2
What is the primary interaction modality for the MVP?

A) Web-based chat interface (browser, desktop or mobile browser)

B) CLI / terminal-based interface

C) API-only (you'll build or connect your own frontend later)

D) Web-based chat interface with API available for future integrations

X) Other (please describe after [Answer]: tag below)

[Answer]: C)

---

## Question 3
How should the system handle conversation flow with regard to memory extraction?

A) Fully automatic — the system silently decides what to remember from every conversation without asking

B) Confirm-then-store — the system proposes what it wants to remember and the user approves/rejects before storage

C) Hybrid — automatic extraction for obvious facts/events, but confirmation for interpretations or uncertain information

D) User-triggered — memory is only stored when the user explicitly says "remember this" or similar

X) Other (please describe after [Answer]: tag below)

[Answer]: I'm inclined towards A, but i wanted it be sure that it doesnt leave off important pieces 

---

## Question 4
What is your tolerance for latency in responses when historical context retrieval is involved?

A) Fast responses are critical — accept some loss of retrieval depth to keep response time under 3-5 seconds

B) Moderate latency acceptable — up to 10-15 seconds is fine if it means better context retrieval

C) Accuracy over speed — I'd wait 20-30+ seconds for a thorough retrieval if it means the answer is historically correct

D) Adaptive — fast for simple questions, allowed to be slower for complex historical queries

X) Other (please describe after [Answer]: tag below)

[Answer]: C)

---

## Question 5
What volume of interaction do you expect in a typical week during the first 6 months?

A) Light — a few conversations per week (5-10 messages each)

B) Moderate — daily conversations (10-30 messages each)

C) Heavy — multiple daily sessions with lengthy conversations (50+ messages per day)

X) Other (please describe after [Answer]: tag below)

[Answer]: C)

---

## Question 6
Where should the system run for the MVP deployment?

A) Entirely local (your own machine, all data stays on disk, no cloud services except LLM API calls)

B) Self-hosted on a personal cloud instance (VPS/EC2) — you manage the server

C) Docker Compose on local or cloud, but with cloud-hosted database services (managed Neo4j Aura, managed PostgreSQL, etc.)

D) Local development with the option to deploy to a personal cloud instance later

X) Other (please describe after [Answer]: tag below)

[Answer]: A). As of now local, After full development, we'll go through that again

---

## Question 7
What is your initial preference for the LLM provider for the primary reasoning/response generation?

A) OpenAI (GPT-4o / GPT-4.1 family) — strong function calling, widely supported

B) Anthropic (Claude 4 family) — strong reasoning, large context window

C) Google (Gemini 2.x) — competitive pricing, multimodal

D) Start with one provider but architect for easy switching; pick whichever is cheapest to prototype with

E) Use different providers for different tasks (e.g., Claude for reasoning, OpenAI for embeddings, local model for classification)

X) Other (please describe after [Answer]: tag below)

[Answer]: D), I really like Gemini, but pricing is somehting why I'd prefer to move to other models 

---

## Question 8
How important is the ability to operate fully offline (no external API calls) for the MVP?

A) Not important — cloud LLM APIs are acceptable for all operations

B) Somewhat important — core memory storage/retrieval should work offline, but LLM responses can require connectivity

C) Very important — I want the option to run entirely with local models (Ollama/llama.cpp) even if quality is lower

D) Hybrid — local models for memory extraction/classification, cloud models for final response generation

X) Other (please describe after [Answer]: tag below)

[Answer]: A) (Maybe will have some changes later on)

---

## Question 9
For the knowledge graph (entities, relationships, temporal facts), what is your preference on graph database infrastructure?

A) Neo4j Community Edition (self-hosted, Docker) — full-featured graph database, established ecosystem

B) Neo4j Aura Free/Pro (managed cloud) — less operational burden, but data leaves your machine

C) Lightweight embedded alternative (e.g., SQLite + JSON for graph-like queries, or NetworkX in-memory) — simpler but less capable

D) Start with PostgreSQL (JSONB + recursive CTEs for graph traversal) and migrate to Neo4j only if needed

E) Evaluate Graphiti's requirements and use whatever it needs — let the memory framework dictate the graph layer

X) Other (please describe after [Answer]: tag below)

[Answer]: X) not sure, please decide based on Accuracy on priority and free ( as the initial product is personal use)

---

## Question 10
What types of personal context are most critical for the MVP to handle correctly? (This helps prioritize the memory model.)

A) People and relationships (family, friends, colleagues — who they are, how they relate, what's happening with them)

B) Events and timelines (what happened when, chronological sequences, evolving situations)

C) Decisions and reasoning (why you chose X, what alternatives were considered, what the outcome was)

D) All three are equally critical — the system needs to handle people, events, and decisions from day one

X) Other (please describe after [Answer]: tag below)

[Answer]: D)

---

## Question 11
How should the system handle the "memory inspection" UI in the MVP?

A) Minimal — a simple searchable list/timeline view of stored memories and entities, read-only

B) Moderate — browse, search, edit, and delete memories; view entity graphs; see provenance chains

C) Rich — full knowledge graph visualization, timeline explorer, relationship maps, memory diff/history

D) Start with API endpoints for memory inspection; build a basic UI later once the backend is proven

X) Other (please describe after [Answer]: tag below)

[Answer]: D)

---

## Question 12
How should the system handle imports of existing context? (e.g., bringing in history from other tools)

A) Not needed for MVP — all context will be built from new conversations

B) Basic text/markdown import — paste or upload documents that get processed into the memory system

C) Structured import from specific sources (e.g., journal entries, notes apps, exported chat logs)

D) Important but can be deferred to post-MVP

X) Other (please describe after [Answer]: tag below)

[Answer]: B)

---

## Question 13
What is your expectation for how the system handles multi-turn memory correction?

A) Simple overwrite — "forget X" removes it, "actually it was Y" replaces it

B) Correction with history — corrections create a new version but the original is preserved in an audit trail

C) Full temporal correction — the system tracks what was believed at each point in time and can answer "what did I think was true in March?"

X) Other (please describe after [Answer]: tag below)

[Answer]: C)

---

## Question 14
How should the evaluation framework work in the MVP?

A) Manual testing only — you'll judge correctness subjectively during normal use

B) A set of scripted synthetic scenarios that can be run on-demand to test retrieval and memory correctness

C) Automated regression suite with synthetic personas/histories that runs as part of CI/CD

D) Start with B (scripted scenarios) and evolve toward C over time

X) Other (please describe after [Answer]: tag below)

[Answer]: Will check later 

---

## Question 15
What is your budget/cost tolerance for external services during development and early use?

A) Minimal — prefer free tiers and open-source; keep monthly costs under $20

B) Moderate — willing to spend $50-100/month on LLM APIs and managed services during development

C) Flexible — willing to spend what's needed to get the best quality; cost optimization comes later

D) Cost-conscious but practical — use paid APIs for quality, but architect to minimize unnecessary calls

X) Other (please describe after [Answer]: tag below)

[Answer]: Quality first, only planning to pay for LLM model APIs rest stack should be free

---

## Question 16: Security Extensions
Should security extension rules be enforced for this project?

A) Yes — enforce all SECURITY rules as blocking constraints (recommended for production-grade applications)

B) No — skip all SECURITY rules (suitable for PoCs, prototypes, and experimental projects)

X) Other (please describe after [Answer]: tag below)

[Answer]: B)

---

## Question 17: Resiliency Extensions
Should the resiliency baseline be applied to this project?

**What this extension is.** Enabling it applies a set of directional, design-time best practices for building resilient systems, derived from the AWS Well-Architected Framework (Reliability Pillar) and resilience-review guidance. It steers requirements, design, and code toward fault tolerance, high availability, observability, and recoverability.

**What this extension is NOT.** Enabling it does not make your workload production-ready, nor does it certify or guarantee any availability, RTO, or RPO target. It is a starting point that scaffolds good resiliency decisions early.

A) Yes — apply the resiliency baseline as directional best practices and design-time guidance (recommended for business-critical workloads)

B) No — skip the resiliency baseline (suitable for PoCs, prototypes, and experimental projects where rapid iteration matters more than reliability)

X) Other (please describe after [Answer]: tag below)

[Answer]: Not sure, I dont want the project built with mess I want standards, even tho it is an experminetal project

---

## Question 18: Property-Based Testing Extension
Should property-based testing (PBT) rules be enforced for this project?

A) Yes — enforce all PBT rules as blocking constraints (recommended for projects with business logic, data transformations, serialization, or stateful components)

B) Partial — enforce PBT rules only for pure functions and serialization round-trips (suitable for projects with limited algorithmic complexity)

C) No — skip all PBT rules (suitable for simple CRUD applications, UI-only projects, or thin integration layers with no significant business logic)

X) Other (please describe after [Answer]: tag below)

[Answer]: I dont know whtat that is, if it's not a Hurdle ignore this and move ahead will take care of this later, 

---
