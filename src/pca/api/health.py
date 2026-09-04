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

    # Extraction status by state, plus locally in-flight tasks (Unit 5).
    #
    # Distinct from the episode backlog above: an episode can be ingested into the
    # graph while its extraction is still pending, and a coordinator that has stopped
    # draining is otherwise completely invisible — the API keeps returning 200 and
    # replies look normal while memory silently stops accumulating.
    try:
        extraction = await container.coordinator.backlog()
    except Exception:  # noqa: BLE001 - health must never raise
        extraction = {}

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
        DependencyHealth(
            name="extraction",
            # FAILED is the state that means something is actually wrong. PENDING and
            # RUNNING are normal transient states on a working system, so treating a
            # non-zero count of those as unhealthy would make the endpoint cry wolf
            # under ordinary load and train the operator to ignore it.
            healthy=extraction.get("failed", 0) == 0,
            detail=_extraction_detail(extraction),
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


def _extraction_detail(counts: dict[str, int]) -> str:
    """Render the extraction backlog for an operator.

    Reports the counts that indicate a problem or explain a delay, and says so plainly
    when everything is clear. A raw dict dump would technically carry the same
    information while making "is anything wrong?" a question the reader has to work
    out for themselves.
    """
    if not counts:
        return "extraction status unavailable"

    failed = counts.get("failed", 0)
    pending = counts.get("pending", 0)
    running = counts.get("running", 0)
    abandoned = counts.get("abandoned", 0)
    in_flight = counts.get("in_flight_local", 0)

    if failed:
        return (
            f"{failed} extraction(s) FAILED and will not retry without "
            f"intervention; {pending} pending, {running} running"
        )

    parts = [f"{pending} pending", f"{running} running", f"{in_flight} in flight here"]
    if abandoned:
        # Abandoned is recoverable, not broken — the barrier stopped waiting and the
        # reader proceeded with a disclosure. Worth reporting, not worth alarming.
        parts.append(f"{abandoned} abandoned (recoverable at next restart)")
    return ", ".join(parts)
