"""The demo control endpoints, and the fact that they do not exist unless enabled."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import requires_postgres

pytestmark = [requires_postgres, pytest.mark.integration]


@pytest.fixture
def demo_client(engine, session):
    """A client with DEMO_MODE on, built the way the deployed app is."""
    from app.api.deps import get_db
    from app.api.main import create_app
    from app.config import Settings, get_settings
    from app.queue import InMemoryPublisher

    get_settings.cache_clear()
    original = get_settings

    def demo_settings() -> Settings:
        return Settings(demo_mode=True, component="test")

    import app.api.main as main_module

    main_module.get_settings = demo_settings  # type: ignore[assignment]
    try:
        def override_get_db():
            yield session

        app = create_app()
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[original] = demo_settings
        with TestClient(app) as client:
            client.app.state.publisher = InMemoryPublisher()
            yield client
        app.dependency_overrides.clear()
    finally:
        main_module.get_settings = original  # type: ignore[assignment]
        get_settings.cache_clear()


def test_demo_routes_do_not_exist_when_disabled(client: TestClient) -> None:
    """With DEMO_MODE off the endpoints are not registered, so they cannot be reached at all."""
    assert client.get("/api/demo/state").status_code == 404
    assert client.post("/api/demo/llm-failure", json={"mode": "timeout"}).status_code == 404

    paths = client.get("/openapi.json").json()["paths"]
    assert not any(path.startswith("/api/demo") for path in paths)


def test_state_starts_empty(demo_client: TestClient) -> None:
    body = demo_client.get("/api/demo/state").json()
    assert body["total_armed"] == 0
    assert sorted(body["modes"]) == ["http_500", "malformed", "timeout"]


def test_arming_and_clearing(demo_client: TestClient) -> None:
    armed = demo_client.post("/api/demo/llm-failure", json={"mode": "timeout", "count": 3}).json()
    assert armed["armed"]["timeout"] == 3
    assert armed["total_armed"] == 3

    assert demo_client.get("/api/demo/state").json()["total_armed"] == 3

    cleared = demo_client.delete("/api/demo/llm-failure").json()
    assert cleared["total_armed"] == 0


def test_arming_replaces_the_previous_count(demo_client: TestClient) -> None:
    demo_client.post("/api/demo/llm-failure", json={"mode": "http_500", "count": 5})
    body = demo_client.post("/api/demo/llm-failure", json={"mode": "http_500", "count": 1}).json()
    assert body["armed"]["http_500"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "nonsense"},
        {"mode": "timeout", "count": -1},
        {"mode": "timeout", "count": 999},
        {"count": 1},
    ],
)
def test_invalid_arm_requests_are_rejected(demo_client: TestClient, payload: dict) -> None:
    assert demo_client.post("/api/demo/llm-failure", json=payload).status_code == 422


def test_health_details_surfaces_armed_failures(demo_client: TestClient) -> None:
    """A demo left armed must not look like a real outage to the next person."""
    demo_client.post("/api/demo/llm-failure", json={"mode": "malformed", "count": 2})

    config = demo_client.get("/api/health/details").json()["config"]
    assert config["demo_mode"] is True
    assert config["demo_failures_armed"] == 2
