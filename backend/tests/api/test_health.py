"""Probe semantics.

Liveness must not depend on external dependencies: an OpenAI or database blip should show up as
failed work in metrics, not as Kubernetes restarting healthy processes.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.main import create_app
from tests.conftest import requires_postgres


def test_liveness_does_not_touch_the_database() -> None:
    """No fixtures, no database: /healthz answers from the process alone."""
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@requires_postgres
def test_readiness_reports_ok_when_the_database_answers(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_readiness_fails_closed_when_the_database_is_unreachable(monkeypatch) -> None:
    """Readiness is what removes a pod from the load balancer, so it must fail closed."""
    import app.api.main as main_module

    monkeypatch.setattr(main_module, "ping", lambda session: False)
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["dependency"] == "postgres"
