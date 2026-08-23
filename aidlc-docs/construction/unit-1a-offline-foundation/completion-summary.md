# Unit 1a — Offline Foundation: Completion Summary

**Status**: COMPLETE
**Date**: 2026-08-22
**Verification**: 53 unit tests passing, 12/12 live integration checks passing

---

## What was built

| Area | Files |
|---|---|
| Project setup | `pyproject.toml` (exact pins, Python 3.13), `.env.example`, `.env` |
| Domain (L0) | `enums.py`, `ids.py`, `temporal.py`, `conversation.py`, `memory.py`, `retrieval.py`, `errors.py` |
| Temporal engine | `services/time_resolver.py` |
| Ports (L4) | `clock.py`, `store.py`, `llm.py`, `graph.py`, `objects.py` |
| Adapters (L5) | `adapters/clock/system_clock.py`, `adapters/gemini/provider.py` |
| Cross-cutting | `config/settings.py`, `observability/logging.py` |
| Schema | `migrations/0001_foundation.sql` (authored, not applied) |
| Infrastructure | `docker-compose.yml` (authored, not run) |
| Test doubles | `tests/fakes/` — clock, llm, graph, store, object store |
| Tests | `tests/unit/test_time_resolver.py` — 53 cases |
| Verification scripts | `scripts/list_models.py`, `scripts/verify_gemini.py`, `scripts/verify_small_and_embed.py`, `scripts/verify_unit1a.py` |

---

## Completion criteria — met

| Criterion | Result |
|---|---|
| `TimeResolver` passes exhaustive tests including DST and unresolvable cases | 53 passed |
| Domain types and ports import with no dependency cycles | Verified |
| `GeminiProviderAdapter` returns a real completion against the live API | Verified, 1.6 s |
| Nothing requires a database | Confirmed — entire unit runs with no container |

---

## The finding that justified this unit's ordering

**Every Gemini model identifier in Graphiti's documentation is dead.**

`gemini-2.0-flash` is not offered. The whole `gemini-2.5-*` family returns 404 for new keys. `embedding-001` does not exist. Had these been trusted rather than verified, Unit 1b would have failed at first contact with an opaque 404 and the cause would have looked like a Graphiti integration problem rather than a stale model name.

More consequentially, model selection turned on a measurement that version numbers would have got backwards:

| Model | Structured-output latency |
|---|---|
| `gemini-3.7-flash` | 186.1 s |
| `gemini-3.6-flash` | 34.5 s |
| `gemini-3.5-flash` | **2.9 s** |

The newest model is **sixty times slower** for the operation this system depends on most, and on its own would consume seven times the entire 25-second retrieval budget. Structured output is the decisive capability because both our extraction pipeline and Graphiti's internal entity extraction rely on it.

Final pins: `gemini-3.5-flash` (LLM), `gemini-3.5-flash-lite` (classification and reranking), `gemini-embedding-001` (embeddings, 3072 dimensions).

---

## The ADR-010 contract, proven end to end

The split between model and code was verified working on a real sentence:

```
Input:  "I had a big argument with my sister Priya last Tuesday
         about the house in Pune."

Gemini returns STRUCTURE only:  weekday=1, modifier=last
TimeResolver computes the DATE: 2025-12-29T18:30Z .. 2025-12-30T18:30Z
                                 (= local Tue 30 Dec, Asia/Kolkata)
Granularity: DAY.  Raw phrase "last Tuesday" retained.
```

The model never produced a date, which is the point. Its arithmetic errors would be silent; deterministic arithmetic is testable.

---

## Deviations and knowing exceptions

| Item | Note |
|---|---|
| `python-dotenv` added | Not in the original dependency list. Needed by the verification scripts to read `.env` outside the FastAPI lifecycle |
| AFC disabled in adapter | The GenAI SDK enables automatic function calling by default and warns on every call. We pass no tools, so it is explicitly disabled |
| `.env` contains a live credential | Supplied by the user. Constraint C-8 still applies: this application has no authentication and must bind to localhost only |
| `gemini-embedding-2` not selected | Works, same 3072 dimensions, ~3x faster. Rejected for now because switching embedding models invalidates every stored vector and forces a full reindex; `-001` is the safer compatibility bet with Graphiti |

---

## What Unit 1a deliberately does NOT do

No PostgreSQL adapter, no Graphiti adapter, no services beyond `TimeResolver`, no workflows, no API routes. Those are Unit 1b and are blocked on a container runtime (constraint C-19).

---

## Blocked next step

**Unit 1b requires PostgreSQL and Neo4j 5.26+ running.** No container runtime is installed. `docker-compose.yml` is authored and pinned, ready to run when Docker is available.

Verified absent: `docker`, `docker-compose`, `podman`, Docker Desktop in all standard paths. WSL is present, which is the main prerequisite for Docker Desktop's WSL2 backend.
