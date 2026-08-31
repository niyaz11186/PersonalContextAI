# Personal Context AI

**An AI assistant that actually remembers your life — across weeks, months, and years — and keeps that memory honest over time.**

Personal Context AI (PCA) is a private, self-hosted, single-user assistant with longitudinal memory. You talk to it normally, and it quietly builds a structured, time-aware picture of your world: the people you know, the projects you're on, the places you've lived, and how all of that changes. Later — days or months on — it can pull the right piece of that history back into a conversation without you having to repeat yourself.

---

## The problem

Most AI chat assistants forget everything the moment a conversation ends. The ones that do "remember" tend to keep a flat pile of notes that gets stale, contradicts itself, and can't tell you *when* something was true or *why* it believes what it believes.

Real personal context isn't a snapshot. It's a timeline:

- Your sister moved cities last year. Both facts are true — she *used* to live in one place, and now lives in another.
- You changed jobs. Your old title wasn't wrong; it just stopped being current.
- You told the assistant something, then corrected it. Both the original belief and the correction are part of your history.

A useful memory has to hold all of that at once: what's true now, what *was* true then, what changed, and what the system believed at each point along the way. That's the problem PCA is built to solve.

## The idea

PCA treats memory as a **temporal knowledge graph** instead of a chat log. Every time you talk to it:

1. It stores your exact words as immutable source material.
2. In the background, it extracts facts, events, people, places, and relationships from what you said.
3. It files each of those into a graph with timestamps and validity periods, so nothing overwrites your past.
4. When you ask something later, it retrieves the smallest set of *relevant* history — semantically, by entity, by relationship, and by time — and builds a clear context package before answering.

The guiding principle is **memory integrity over convenience**: newer information never silently deletes older information, and the system never quietly picks a winner when two facts conflict. It surfaces the contradiction instead.

## What makes it different

- **Two separate notions of time.** PCA tracks *when something was true* (valid-from / valid-to) separately from *when the system believed it* (belief history). That's what lets it answer both "Where did my sister live in March?" and "What did I *think* was true back in March?" — different questions with different answers.
- **Facts vs. interpretations stay separate.** What you actually said is tagged differently from what the AI inferred. An inference can never silently graduate into a stated fact.
- **Provenance on everything.** Every memory traces back to the conversation and message it came from, so you can always ask "where did you learn that?"
- **Relative time, done carefully.** Personal context is full of "last Tuesday" and "three weeks ago." The AI identifies the phrase; deterministic code does the actual date math, because language models are unreliable and silently wrong at arithmetic.
- **Corrections are first-class.** You can correct or forget a memory. The original is preserved in an audit trail rather than erased, and the system respects the correction going forward.
- **Private by design.** Everything lives in databases on your own machine. The only data that leaves is what's sent to the language model to generate a reply.

## How it works (at a glance)

```
You ──▶ Conversation API ──▶ store your message (durable)
                                     │
                                     ├─▶ reply now (streamed back)
                                     │
                                     └─▶ background: extract facts, entities,
                                         relationships, and time into the graph

Later question ──▶ retrieve relevant history (semantic + entity + graph + time)
                ──▶ rank it ──▶ build a context package ──▶ answer
```

Under the hood:

- **PostgreSQL** is the system of record — the authoritative, append-only store of everything you said.
- **Neo4j + Graphiti** hold the temporal knowledge graph. This is a *rebuildable projection* of the source material, so a memory-layer problem can never damage your actual history.
- **Google Gemini** provides the language model, embeddings, and reranking.
- **LangGraph** orchestrates the multi-step workflows (normal chat, extraction, correction, historical queries).
- **FastAPI** exposes it all as a streaming API.

## Design principles

- **Correctness over feature count.** A smaller system that remembers accurately beats a bigger one that drifts.
- **Data integrity over aggressive summarization.** History is preserved, not compressed away.
- **Accuracy over speed.** Thorough retrieval is worth a few seconds of latency.
- **Local and private.** Self-hosted, single-user, no data stored with third parties.
- **Evolutionary architecture.** A ports-and-adapters boundary keeps every external dependency replaceable.

## Project status

PCA is being built in stages. It's at an early **walking-skeleton** stage: the conversation API, durable message storage, background extraction into the graph, and relative-time resolution work end to end. Richer capabilities — memory correction and deletion, memory inspection endpoints, hybrid retrieval with reranking, and belief-history/timeline queries — are on the roadmap.

| Working now | On the roadmap |
|---|---|
| Conversation API with streaming responses | Memory correction and deletion |
| Append-only message storage | Memory inspection endpoints |
| Background fact extraction into the graph | Hybrid retrieval and reranking |
| Relative-time resolution ("last Tuesday") | Belief history and timeline queries |
| Per-dependency health checks | Import, export, backup, reindex |

## A note on security

This is a personal, local-first project and **has no authentication.** It's meant to be bound to `127.0.0.1` on your own machine. Because it holds every conversation and every fact it has learned about you (and the people in your life), exposing it to a network without adding authentication would expose all of that. That's a deliberate MVP tradeoff, not an oversight.

## Getting started

The tech stack: Python 3.13, FastAPI, Google Gemini, Neo4j (5.26+) via Graphiti, PostgreSQL, and LangGraph, with PostgreSQL and Neo4j run locally through Docker Compose.

```bash
# 1. Environment
python -m venv venv
.\venv\Scripts\Activate.ps1        # Windows PowerShell
# source venv/bin/activate          # macOS / Linux
pip install -e ".[dev]"

# 2. Configure (set GOOGLE_API_KEY and PCA_NEO4J_PASSWORD)
cp .env.example .env

# 3. Start the databases
docker compose up -d

# 4. Run
python -m uvicorn pca.main:app --host 127.0.0.1 --port 8000
```

Then open http://127.0.0.1:8000/docs. Migrations apply automatically on startup.

Try it:

```bash
# create a conversation
curl -X POST http://127.0.0.1:8000/conversations \
  -H "Content-Type: application/json" -d '{"title":"First"}'

# send a message (streams back)
curl -N -X POST http://127.0.0.1:8000/conversations/<ID>/messages \
  -H "Content-Type: application/json" \
  -d '{"content":"My sister Priya moved to Pune last Tuesday."}'
```

Full setup, model-selection notes, the startup sequence, and troubleshooting live in **[SETUP.md](SETUP.md)**.

## Project layout

```
src/pca/
├── domain/          pure types, stdlib only
├── ports/           abstract interfaces
├── services/        business logic, depends on ports only
├── orchestration/   LangGraph workflows
├── adapters/        graphiti/ gemini/ postgres/ clock/ files/
├── api/             FastAPI routers
├── config/          settings, migration runner
├── observability/   logging and diagnostics
├── composition.py   wires adapters to ports
└── main.py
migrations/          numbered raw SQL, forward-only
tests/               unit/, integration/, fakes/
aidlc-docs/          requirements, design, architecture decisions, audit trail
```

The layout is layer-first, so a boundary violation shows up as a visibly wrong import path. The 17 architecture decisions and the reasoning behind them live in `aidlc-docs/inception/application-design/architecture-decisions.md`.
