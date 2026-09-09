"""Metrics endpoint for the processes that are loops rather than servers.

The worker and ingestor have no HTTP surface of their own, but Prometheus needs somewhere to
scrape. This starts a small daemon thread serving /metrics, which exits with the process.
"""

from __future__ import annotations

from prometheus_client import start_http_server

from app.logging import get_logger

log = get_logger(__name__)

DEFAULT_PORT = 9100


def start(port: int = DEFAULT_PORT, component: str = "worker") -> None:
    """Serve /metrics on ``port``.

    A failure here is logged and swallowed: losing metrics is bad, but taking the worker down and
    stopping all scoring because a port is busy would be worse.
    """
    try:
        start_http_server(port)
        log.info("metrics_server_started", extra={"port": port, "for_component": component})
    except OSError as exc:
        log.error("metrics_server_failed", extra={"port": port, "error": str(exc)})
