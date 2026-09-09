"""Configuration assembly and the guarantee that secrets never reach the logs."""

from __future__ import annotations

import json
import logging

from app.config import Settings
from app.logging import JsonFormatter, configure_logging


def test_database_url_is_assembled_from_parts() -> None:
    # database_url=None on purpose: the test environment exports DATABASE_URL, and an explicit
    # URL is documented to win over the parts.
    settings = Settings(
        database_url=None,
        postgres_host="db",
        postgres_port=5432,
        postgres_db="convoscore",
        postgres_user="app",
        postgres_password="s3cret",
    )
    assert settings.sqlalchemy_url == "postgresql+psycopg://app:s3cret@db:5432/convoscore"


def test_explicit_database_url_wins() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://u:p@host:5432/db", postgres_host="ignored"
    )
    assert settings.sqlalchemy_url == "postgresql+psycopg://u:p@host:5432/db"


def test_password_is_redacted_in_repr_and_summary() -> None:
    settings = Settings(postgres_password="hunter2")

    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings.safe_summary())
    assert settings.postgres_password.get_secret_value() == "hunter2"


def test_safe_summary_contains_no_credentials() -> None:
    summary = Settings(postgres_password="hunter2").safe_summary()
    assert set(summary) == {"environment", "component", "demo_mode", "max_attempts"}


def test_log_lines_are_json_with_structured_fields() -> None:
    formatter = JsonFormatter("worker")
    record = logging.LogRecord(
        name="app.worker", level=logging.INFO, pathname=__file__, lineno=1,
        msg="job_completed", args=(), exc_info=None,
    )
    record.job_id = "abc"
    record.attempt = 2

    payload = json.loads(formatter.format(record))

    assert payload["event"] == "job_completed"
    assert payload["component"] == "worker"
    assert payload["level"] == "info"
    assert payload["job_id"] == "abc"
    assert payload["attempt"] == 2
    assert "ts" in payload


def test_configure_logging_installs_exactly_one_handler() -> None:
    configure_logging("api", "INFO", as_json=True)
    configure_logging("api", "INFO", as_json=True)
    assert len(logging.getLogger().handlers) == 1
