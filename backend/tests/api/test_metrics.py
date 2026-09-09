"""Metrics exposure and, above all, label cardinality.

A label with unbounded values turns one metric into millions of time series and takes down the
monitoring system. The route-template tests below are the guard against the most likely way that
would happen here: labelling requests by resolved path, so every job id becomes its own series.
"""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi.testclient import TestClient

from app import metrics
from tests.conftest import requires_postgres, sample_conversation

pytestmark = [requires_postgres, pytest.mark.integration]


def scrape(client: TestClient) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    return response.text


def series(text: str, metric: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(metric) and "#" not in line]


def value(text: str, needle: str) -> float:
    """Value of the single series containing `needle`, or 0 if it is not present yet.

    Counters live for the life of the process, so tests compare a delta rather than an absolute:
    asserting "== 1" would pass alone and fail as soon as another test ran first.
    """
    for line in series(text, "convoscore_"):
        if needle in line:
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def test_metrics_endpoint_serves_prometheus_text(client: TestClient) -> None:
    body = scrape(client)
    assert "# HELP convoscore_" in body
    assert "# TYPE convoscore_" in body


def route_labels(text: str) -> set[str]:
    """Every distinct value of the `route` label currently in the exposition."""
    return set(re.findall(r'route="([^"]*)"', text))


def test_request_metrics_use_the_route_template_not_the_path(client: TestClient) -> None:
    """The guard against one time series per job id.

    Three requests to three different job ids must produce one route label, not three. The
    assertion is on distinct label *values*, because two requests to the same route with
    different status codes are legitimately two series.
    """
    for _ in range(3):
        client.get(f"/api/jobs/{uuid.uuid4()}")

    body = scrape(client)
    job_routes = {route for route in route_labels(body) if "/jobs" in route}

    assert job_routes == {"/api/jobs/{job_id}"}
    # No raw UUID may appear anywhere in the exposition.
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", body)


def test_unmatched_paths_collapse_to_one_label(client: TestClient) -> None:
    """A scanner hitting random URLs must not be able to create unbounded labels."""
    for path in ("/nope", "/also-nope", "/definitely/not/here"):
        client.get(path)

    routes = route_labels(scrape(client))

    assert "unmatched" in routes
    for path in ("/nope", "/also-nope", "/definitely/not/here"):
        assert path not in routes


def test_probe_and_scrape_traffic_is_excluded(client: TestClient) -> None:
    """Probes fire every few seconds and would otherwise drown out real traffic."""
    client.get("/healthz")
    client.get("/readyz")

    body = scrape(client)
    assert '/healthz' not in body
    assert '/readyz' not in body


def test_request_metrics_record_status_and_duration(client: TestClient) -> None:
    client.get("/api/stats")
    body = scrape(client)

    assert any('route="/api/stats"' in line and 'status="200"' in line
               for line in series(body, "convoscore_http_requests_total"))
    assert series(body, "convoscore_http_request_duration_seconds_bucket")


def test_submitting_a_conversation_counts_a_job(client: TestClient) -> None:
    before = value(scrape(client), 'convoscore_jobs_created_total{source="api"}')
    client.post("/api/conversations", json=sample_conversation())
    after = value(scrape(client), 'convoscore_jobs_created_total{source="api"}')

    assert after == before + 1


def test_enqueue_failure_is_visible_in_metrics(client: TestClient) -> None:
    """The dual-write gap has a counter, not just a log line."""
    from app.queue import FailingPublisher

    before = value(scrape(client), 'convoscore_enqueue_failures_total{source="api"}')

    client.app.state.publisher = FailingPublisher("queue down")
    client.post("/api/conversations", json=sample_conversation())

    body = scrape(client)
    assert value(body, 'convoscore_enqueue_failures_total{source="api"}') == before + 1
    assert any('error_type="enqueue_failed"' in line
               for line in series(body, "convoscore_jobs_failed_total"))


def test_job_state_gauges_are_read_from_the_database(client: TestClient, session) -> None:
    """Backlog is a state. Reading it at scrape time is what makes a stall visible.

    The commit matters: the gauge opens its own session and reports committed state, which is the
    correct behaviour and is what the deployed system does.
    """
    client.post("/api/conversations", json=sample_conversation())
    session.commit()

    body = scrape(client)
    assert 'convoscore_jobs_by_status{status="pending"} 1.0' in body
    # Every status is reported, including zeros: "0 failed" and "no data" read very differently.
    for status in ("processing", "completed", "failed"):
        assert f'convoscore_jobs_by_status{{status="{status}"}} 0.0' in body
    assert series(body, "convoscore_oldest_pending_job_age_seconds")


def test_a_failing_gauge_callback_does_not_break_the_endpoint() -> None:
    """One broken query must not take the whole scrape down with it."""
    from prometheus_client import CollectorRegistry, generate_latest

    gauges = metrics.CallbackGauges()
    gauges.register("convoscore_broken", "raises", [], lambda: 1 / 0)
    gauges.register("convoscore_fine", "works", [], lambda: 7)

    registry = CollectorRegistry()
    registry.register(gauges)
    output = generate_latest(registry).decode()

    assert "convoscore_fine 7.0" in output
    assert "convoscore_broken" not in output


def test_every_metric_label_is_from_a_bounded_set(client: TestClient) -> None:
    """A sweep over the exposition for anything that looks like an unbounded identifier."""
    client.post("/api/conversations", json=sample_conversation())
    body = scrape(client)

    for line in series(body, "convoscore_"):
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}", line), f"uuid in: {line}"
        assert "s3://" not in line, f"object key in: {line}"
