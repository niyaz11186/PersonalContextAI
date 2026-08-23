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

    return HealthResponse(healthy=overall, dependencies=dependencies, note=note)


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    """Process liveness only. Deliberately touches no dependency."""
    return {"status": "alive"}
