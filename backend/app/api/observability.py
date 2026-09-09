"""API instrumentation: request metrics and the /metrics endpoint.

The route *template* is used as a label, never the resolved path. Labelling by path would create
one time series per job id, which is exactly how a monitoring system gets taken down by the thing
it is meant to watch.
"""

from __future__ import annotations

import time

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app import metrics
from app.db import get_session_factory
from app.logging import get_logger
from app.models import JobStatus
from app.repository import count_by_status, ingest_counts, oldest_pending_age_seconds

log = get_logger(__name__)

# Paths that would otherwise dominate the request metrics without saying anything about the
# service: the metrics scrape itself, and the probes Kubernetes runs every few seconds.
EXCLUDED_PATHS = frozenset({"/metrics", "/healthz", "/readyz"})


def route_template(request: Request) -> str:
    """The matched route pattern, e.g. ``/api/jobs/{job_id}``.

    Read from the request scope, which Starlette fills in once it has routed. That is why this is
    called *after* the request has been handled: walking ``app.routes`` instead does not work,
    because an app built from included routers holds router objects there rather than the leaf
    routes, and every request would be labelled "unmatched".

    Anything that never matched a route collapses to a single "unmatched" series, so a scanner
    hitting random URLs cannot create unbounded label values.
    """
    if request.scope.get("route") is None:
        return "unmatched"

    # The matched route object holds its path relative to the router it belongs to ("/jobs/{id}"),
    # so the "/api" prefix is missing. Rebuild the full template by putting the placeholders back
    # into the real path: bounded, and it reads the way the API is actually documented.
    params: dict[str, object] = request.scope.get("path_params") or {}
    template = request.url.path
    for name, captured in params.items():
        template = template.replace(str(captured), "{" + name + "}", 1)
    return template


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path in EXCLUDED_PATHS:
            return await call_next(request)

        method = request.method
        started = time.perf_counter()
        metrics.http_requests_in_progress.inc()

        try:
            response = await call_next(request)
            status = str(response.status_code)
        except Exception:
            # An unhandled exception is still a request that happened, and a 500 in the metrics is
            # how it becomes visible.
            status = "500"
            self._record(method, route_template(request), status, started)
            raise
        finally:
            metrics.http_requests_in_progress.dec()

        self._record(method, route_template(request), status, started)
        return response

    @staticmethod
    def _record(method: str, template: str, status: str, started: float) -> None:
        elapsed = time.perf_counter() - started
        metrics.http_requests_total.labels(method=method, route=template, status=status).inc()
        metrics.http_request_duration_seconds.labels(method=method, route=template).observe(elapsed)


def _jobs_by_status() -> dict[str, int]:
    """Current job counts. Every status is reported, including zeros.

    Omitting a zero would make a panel show "no data" rather than "none failed", which reads very
    differently at three in the morning.
    """
    session = get_session_factory()()
    try:
        counts = count_by_status(session)
    finally:
        session.close()
    return {status.value: counts.get(status.value, 0) for status in JobStatus}


def _oldest_pending_age() -> float:
    session = get_session_factory()()
    try:
        return oldest_pending_age_seconds(session)
    finally:
        session.close()


def _ingested_objects_by_status() -> dict[str, int]:
    session = get_session_factory()()
    try:
        return ingest_counts(session)
    finally:
        session.close()


# Registered once per process. The Prometheus registry is global, so registering the same
# collector twice raises; building more than one app in a process (tests do, and so would any
# factory called twice) must not be a crash.
_gauges_registered = False


def _install_database_gauges() -> None:
    """Gauges read from PostgreSQL at scrape time.

    The API is the natural home for them: it already owns database reads, and it runs as a single
    replica, so each value is reported once. The dashboard still aggregates with max(), so scaling
    the API out would not silently multiply the backlog.
    """
    global _gauges_registered
    if _gauges_registered:
        return

    gauges = metrics.CallbackGauges()
    gauges.register(
        "convoscore_jobs_by_status",
        "Jobs currently in each state.",
        ["status"],
        _jobs_by_status,
    )
    gauges.register(
        "convoscore_oldest_pending_job_age_seconds",
        "Age of the oldest job that has not finished. The backlog signal.",
        [],
        _oldest_pending_age,
    )
    gauges.register(
        "convoscore_ingested_objects",
        "Storage objects recorded, by outcome.",
        ["status"],
        _ingested_objects_by_status,
    )

    from prometheus_client import REGISTRY

    REGISTRY.register(gauges)
    _gauges_registered = True


def install(app: FastAPI) -> None:
    """Add request metrics, the database-derived gauges, and the /metrics endpoint."""
    app.add_middleware(MetricsMiddleware)
    _install_database_gauges()

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics() -> Response:
        return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE)
