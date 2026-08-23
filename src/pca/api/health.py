"""Health endpoints.

Layer L1. NFR-06.6 — per-dependency checks, not a single boolean.

A flat "healthy: true/false" is close to useless for diagnosis. Reporting each
dependency separately distinguishes "Neo4j is down, retrieval will degrade" from
"PostgreSQL is down, nothing works" — which have very different responses.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from pca.api.schemas import DependencyHealth, HealthResponse
from pca.composition import Container

router = APIRouter(tags=["health"])


def _container(request: Request) -> Container:
    return request.app.state.container


@router.get("/health", response_model=HealthResponse)
async def health(request: Request, response: Response) -> HealthResponse:
    container = _container(request)

    postgres_ok = await container.store.health()
    neo4j_ok = await container.graph.health()
    provider = await container.provider.health()

    # Backlog of episodes persisted but never ingested into the graph.
    #
    # This check exists because a broken ingestion pipeline was previously
    # invisible: every request returned 200, replies looked normal, and the
    # assistant simply said it had no history — which is indistinguishable from
    # genuinely having none. A non-zero backlog is the signal that retrieval is
    # answering from less than it should.
    try:
        backlog = await container.episodes.pending_count()
    except Exception:  # noqa: BLE001 - health must never raise
        backlog = -1

    dependencies = [
        DependencyHealth(
            name="postgres",
            healthy=postgres_ok,
            detail="system of record; no degradation path (C-22)",
        ),
        DependencyHealth(
            name="neo4j",
            healthy=neo4j_ok,
            detail="rebuildable projection; retrieval degrades if down",
        ),
        DependencyHealth(
            name="gemini",
            healthy=provider.healthy,
            detail=provider.detail or provider.model,
        ),
        DependencyHealth(
            name="memory_ingestion",
            healthy=backlog == 0,
            detail=(
                "all episodes ingested"
                if backlog == 0
                else f"{backlog} episode(s) persisted but NOT searchable; restart retries them"
                if backlog > 0
                else "backlog could not be determined"
            ),
        ),
    ]

    # PostgreSQL is the only hard dependency. Neo4j or Gemini being down means
    # reduced capability with disclosure, not an unusable service (NFR-06.5), so
    # they do not on their own make the service unhealthy.
    overall = postgres_ok
    if not overall:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    note = None
    if postgres_ok and not (neo4j_ok and provider.healthy):
        note = "Serving in degraded mode; replies will disclose missing history."
    elif backlog > 0:
        note = (
            f"{backlog} episode(s) are stored but not searchable. Memory written "
            "during that period will not be recalled until they are re-ingested."
        )

    return HealthResponse(healthy=overall, dependencies=dependencies, note=note)


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    """Process liveness only. Deliberately touches no dependency."""
    return {"status": "alive"}
