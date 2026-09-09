"""FastAPI application factory, probes and startup wiring."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status
from sqlalchemy.exc import SQLAlchemyError

from app.api import conversations, demo, observability, stats
from app.config import Settings, get_settings
from app.db import get_engine, get_session_factory, ping
from app.factories import build_queue
from app.logging import configure_logging, get_logger
from app.queue import JobPublisher, SqsPublisher

log = get_logger(__name__)

DESCRIPTION = """
ConvoScore accepts customer-support conversations, scores them asynchronously with an LLM, and
makes the results reviewable.

Submitting a conversation returns `202 Accepted` with a `job_id`; poll `GET /api/jobs/{job_id}`
for `pending → processing → completed | failed`. Conversations that arrive through object storage
follow exactly the same pipeline and appear here with `source = s3`.
""".strip()


def build_publisher(settings: Settings) -> JobPublisher:
    """Choose how job ids are enqueued.

    The queue URL is resolved lazily on first publish, not here: the API must start and serve
    reads even if the queue is temporarily unreachable. A failed publish surfaces as a 503 on that
    one request rather than a pod that will not start.
    """
    queue = build_queue(settings)
    log.info("queue_configured", extra={"queue": settings.sqs_queue_name})
    return SqsPublisher(queue)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.component, settings.log_level, settings.log_json)
    app.state.settings = settings
    app.state.publisher = build_publisher(settings)
    log.info("api_starting", extra=settings.safe_summary())
    yield
    get_engine().dispose()
    log.info("api_stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="ConvoScore",
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
        openapi_url="/openapi.json",
        docs_url="/docs",
    )

    app.include_router(conversations.router, prefix="/api")
    app.include_router(stats.router, prefix="/api")

    # Demo controls exist only when explicitly enabled: with DEMO_MODE off the routes are not
    # registered at all, so they cannot be reached even by accident.
    if settings.demo_mode:
        app.include_router(demo.router, prefix="/api")
        log.warning("demo_mode_enabled", extra={"detail": "failure injection endpoints mounted"})

    # Request metrics, database-derived gauges and /metrics.
    observability.install(app)

    @app.get("/healthz", tags=["health"], summary="Liveness")
    def healthz() -> dict[str, str]:
        """Is this process alive?

        Deliberately checks nothing external. A database or OpenAI outage must not cause
        Kubernetes to restart a process that is working correctly; that would turn a dependency
        blip into a restart storm.
        """
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"], summary="Readiness")
    def readyz(response: Response) -> dict[str, str]:
        """Should this pod receive traffic?

        Only PostgreSQL is checked, because no endpoint can do useful work without it. The queue
        and object store are reported through /api/health/details instead: they degrade specific
        features rather than making the API useless.
        """
        session = get_session_factory()()
        try:
            healthy = ping(session)
        except SQLAlchemyError:
            healthy = False
        finally:
            session.close()

        if not healthy:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "unavailable", "dependency": "postgres"}
        return {"status": "ready"}

    return app


app = create_app()
