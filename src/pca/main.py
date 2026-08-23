"""FastAPI application entry point.

Layer L1.

SECURITY (constraint C-8): this application has **no authentication**. The security
extension was opted out and deployment is single-user and local. It therefore must
bind to 127.0.0.1 only. Exposing it on a network interface would expose the entire
personal context store — every conversation, every extracted fact about the user
and about other people — to anyone who can reach the port.

Run:  .\\venv\\Scripts\\python.exe -m uvicorn pca.main:app --host 127.0.0.1 --port 8000
      (requires PostgreSQL and Neo4j 5.26+ to be running)
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from pca.api import conversation, health
from pca.composition import build_container, start, stop
from pca.observability.logging import get_logger

_log = get_logger(__name__)


def create_app(container=None) -> FastAPI:  # type: ignore[no-untyped-def]
    """Build the application.

    `container` is injectable so the API surface can be exercised against fakes
    with no PostgreSQL, no Neo4j, and no startup I/O. Without this seam the routes
    could not be tested at all until a container runtime exists — which, given
    Docker is unavailable, would mean shipping the API layer unverified.
    """
    preassembled = container

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if preassembled is not None:
            # Injected container: skip build and the startup sequence entirely.
            app.state.container = preassembled
            yield
            return

        built = build_container()
        app.state.container = built
        await start(built)
        try:
            yield
        finally:
            await stop(built)

    application = FastAPI(
        title="Personal Context AI",
        version="0.1.0",
        summary="A private, persistent personal-context assistant with temporal memory.",
        description=(
            "Single-user and unauthenticated by design. Bind to localhost only.\n\n"
            "Walking skeleton: conversation and streaming replies are live. Memory "
            "correction, deletion, inspection, import, export, and hybrid retrieval "
            "arrive in later units."
        ),
        lifespan=lifespan,
    )

    application.include_router(health.router)
    application.include_router(conversation.router)

    @application.get("/", tags=["meta"])
    async def root() -> dict[str, object]:
        return {
            "name": "Personal Context AI",
            "version": "0.1.0",
            "stage": "Unit 1b walking skeleton",
            "authentication": "none — bind to localhost only (constraint C-8)",
            "docs": "/docs",
        }

    return application


app = create_app()
