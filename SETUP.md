# Setup

Personal Context AI — a private, persistent personal-context assistant with
temporal memory. Single user, self-hosted, local only.

> **SECURITY: this application has no authentication.** Bind it to `127.0.0.1`
> only. Exposing the port on a network interface would expose every conversation
> and every extracted fact about you and about other people. This is a deliberate
> MVP decision (constraint C-8), not an oversight.

---

## What runs today

| Working now | Not yet built |
|---|---|
| Conversation API with SSE streaming | Memory correction and deletion |
| Append-only message storage | Memory inspection endpoints |
| Episode persistence and graph ingestion | Hybrid retrieval and reranking |
| Relative-time resolution ("last Tuesday") | Belief history and timeline queries |
| Per-dependency health checks | Import, export, backup, reindex |

Current stage: **Unit 1b walking skeleton.** Units 2–7 add the rest.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.13 | Pinned. `graphiti-core` requires `>=3.10,<4` |
| Docker + Docker Compose | For PostgreSQL and Neo4j |
| A Google Gemini API key | The only paid dependency |

On Windows, Docker Desktop needs the WSL2 backend.

---

## Steps

### 1. Create the environment

```bash
python -m venv venv

# Windows PowerShell
.\venv\Scripts\Activate.ps1
# macOS / Linux
source venv/bin/activate

pip install -e ".[dev]"
```

`tzdata` is a real dependency, not padding. Windows ships no IANA timezone
database, so `zoneinfo` cannot resolve any zone without it and every temporal
operation fails.

### 2. Configure

```bash
cp .env.example .env
```

Then edit `.env`:

| Variable | Notes |
|---|---|
| `GOOGLE_API_KEY` | Required |
| `PCA_USER_TIMEZONE` | Your IANA zone, e.g. `Asia/Kolkata`. Affects how "last Tuesday" resolves |
| `PCA_NEO4J_PASSWORD` | Required. Compose refuses to start without it |

Leave the model pins alone unless you have a reason — they were selected on
measured latency, not version number. See "Model choice" below.

### 3. Start the databases

```bash
docker compose up -d
```

Wait for both to report healthy:

```bash
docker compose ps
```

Neo4j is pinned to `5.26-community` because Graphiti requires 5.26 or newer. The
application also checks the server version at startup and refuses to run against
an older one, since the alternative is an opaque failure at first query.

### 4. Run

```bash
python -m uvicorn pca.main:app --host 127.0.0.1 --port 8000
```

Migrations apply automatically on startup. Open http://127.0.0.1:8000/docs.

### 5. Verify

```bash
# unit and integration tests — no databases required
python -m pytest -q

# live Gemini check
python scripts/verify_unit1a.py
```

---

## Try it

```bash
# create a conversation
curl -X POST http://127.0.0.1:8000/conversations \
  -H "Content-Type: application/json" -d '{"title":"First"}'

# send a message (streams back over SSE)
curl -N -X POST http://127.0.0.1:8000/conversations/<ID>/messages \
  -H "Content-Type: application/json" \
  -d '{"content":"My sister Priya moved to Pune last Tuesday."}'
```

---

## Startup sequence

Each step can deliberately fail the boot, so a misconfiguration surfaces
immediately rather than mid-conversation:

1. Configuration validated — missing API key or Neo4j password fails here
2. Migration checksums verified — an edited applied migration fails here
3. Pending migrations applied, one transaction per file
4. Neo4j version gate — older than 5.26 fails here
5. Graphiti indices built (idempotent)
6. Pending episodes re-ingested — recovers work lost to a crash

---

## Model choice

Pins in `.env.example` were verified against the live API, and selected on
**structured-output latency** rather than version number. Structured output is the
decisive capability: both our extraction and Graphiti's internal entity extraction
depend on it.

| Model | Structured output | Verdict |
|---|---|---|
| `gemini-3.7-flash` | 186 s | Rejected — 7x the whole retrieval budget |
| `gemini-3.6-flash` | 34.5 s | Rejected |
| `gemini-3.5-flash` | 2.9 s | **Selected** |

The whole `gemini-2.5-*` family returns 404 for new keys, so every model name in
Graphiti's published documentation is stale. Re-run `scripts/list_models.py` if
these stop working.

Changing `PCA_EMBEDDING_MODEL` invalidates every stored vector and requires a full
graph rebuild. Do not change it casually.

---

## Privacy

- All data stays in your local PostgreSQL and Neo4j. Nothing else is stored remotely.
- The only outbound traffic is to the Gemini API: message content, retrieved
  context, and text for embedding.
- **Graphiti telemetry is disabled.** `graphiti-core` ships PostHog analytics that
  are on by default; the application sets `GRAPHITI_TELEMETRY_ENABLED=false` at
  import time, and a test asserts it stays that way.
- The `openai` package is installed as a transitive dependency of `graphiti-core`.
  No OpenAI key is configured and no OpenAI call is made — a test enforces both.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `ZoneInfoNotFoundError` | `tzdata` missing. `pip install tzdata` |
| `Neo4j <version> is too old` | Image is not 5.26+. Check `docker-compose.yml` |
| `missing required configuration` | `GOOGLE_API_KEY` or `PCA_NEO4J_PASSWORD` unset |
| `migration ... was modified after being applied` | An applied migration was edited. Migrations are forward-only — add a new one |
| `404 ... no longer available` from Gemini | Model retired. Run `scripts/list_models.py` and update `.env` |
| 503 on every request | PostgreSQL is down. It is the system of record and has no degradation path |
| Replies mention missing history | Neo4j or Gemini degraded. Working as designed — check `/health` |

---

## Layout

```
src/pca/
├── domain/          pure types, stdlib only
├── ports/           abstract interfaces
├── services/        business logic, depends on ports only
├── orchestration/   LangGraph workflows — the only place langgraph is imported
├── adapters/        graphiti/ gemini/ postgres/ clock/ files/
├── api/             FastAPI routers
├── config/          settings, migration runner
├── composition.py   the only place adapters are wired to ports
└── main.py
migrations/          numbered raw SQL, forward-only
tests/               unit/, integration/, fakes/
aidlc-docs/          requirements, design, ADRs, audit trail
```

Layer-first so a boundary violation shows up as a visibly wrong import path.
`aidlc-docs/inception/application-design/architecture-decisions.md` holds the 17
architecture decisions and the reasoning behind them.

---

## Moving to another machine

Do not copy `venv/` — it is machine-specific. Recreate it with step 1.

`.env` contains a live API key. Move it separately or recreate it from
`.env.example`, and rotate the key if it has been exposed.

The following are residue from earlier reverted work and are not used by the
application: `requirements.txt`, `api_test.py`, `key_test.py`, `old.zip`,
`app/`, `backend/`, `UI/`. Safe to leave behind.
