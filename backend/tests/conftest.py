"""Test fixtures.

Tests run against a real PostgreSQL (docker compose -f docker-compose.test.yaml up -d) because the
reliability logic depends on PostgreSQL semantics: conditional UPDATE ... RETURNING, server-side
now(), JSONB and check constraints. A SQLite stand-in would test something we do not ship.

No test ever calls OpenAI.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://convoscore:convoscore@127.0.0.1:55432/convoscore_test",
)

# Settings are read at import time by the application, so they must be set before app modules load.
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("COMPONENT", "test")


def _database_available(url: str) -> bool:
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 3})
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not _database_available(TEST_DATABASE_URL),
    reason=(
        "PostgreSQL is not reachable at TEST_DATABASE_URL. "
        "Start it with: docker compose -f docker-compose.test.yaml up -d"
    ),
)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    from alembic import command
    from app.migrate import alembic_config

    engine = create_engine(TEST_DATABASE_URL, future=True)
    # Apply the real migrations rather than create_all: this also tests that they work.
    config = alembic_config(TEST_DATABASE_URL)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        connection.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A clean database per test."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE conversations"))
    db = factory()
    try:
        yield db
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client(engine: Engine, session: Session) -> Iterator[TestClient]:
    """API client sharing the test's session, with an in-memory publisher on app.state."""
    from app.api.deps import get_db
    from app.api.main import create_app
    from app.queue import InMemoryPublisher

    app = create_app()

    def override_get_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        test_client.app.state.publisher = InMemoryPublisher()
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def publisher(client: TestClient):
    return client.app.state.publisher


def sample_conversation(**overrides) -> dict:
    payload = {
        "messages": [
            {"role": "customer", "content": "My order never arrived and nobody has replied."},
            {"role": "agent", "content": "I am sorry about that, let me check the tracking."},
            {"role": "customer", "content": "This is the third time I have had to chase you."},
        ],
        "metadata": {"channel": "email", "tags": ["delivery"]},
    }
    payload.update(overrides)
    return payload


def new_job_id() -> uuid.UUID:
    return uuid.uuid4()
