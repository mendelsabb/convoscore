"""Database engine, sessions, and the startup wait used by the worker and ingestor."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.logging import get_logger

log = get_logger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def create_db_engine(settings: Settings, statement_timeout_ms: int | None = None) -> Engine:
    """Build an engine.

    ``statement_timeout_ms`` overrides the configured default. Pass 0 to disable the timeout for
    schema migrations, which may legitimately run far longer than any request-path query and must
    never be killed half way through.
    """
    timeout = (
        settings.db_statement_timeout_ms if statement_timeout_ms is None else statement_timeout_ms
    )
    return create_engine(
        settings.sqlalchemy_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        # Recycle connections that a restarted PostgreSQL pod has silently dropped.
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={
            "options": f"-c statement_timeout={timeout}",
            "application_name": f"convoscore-{settings.component}",
        },
        future=True,
    )


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_db_engine(get_settings())
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _session_factory


def reset_engine() -> None:
    """Drop cached engine/session factory. Used by tests and after a config change."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transaction boundary: commit on success, roll back on any exception."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ping(session: Session) -> bool:
    """Cheap liveness check for the readiness probe."""
    try:
        session.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False


def wait_for_database(
    engine: Engine, timeout_seconds: float = 180.0, interval: float = 2.0
) -> None:
    """Block until PostgreSQL accepts a connection, or raise once the timeout elapses.

    The migration init container and every worker call this, so a slow-starting database produces
    a waiting pod with a clear log line rather than a crash loop.
    """
    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            log.info("database_ready", extra={"attempt": attempt})
            return
        except SQLAlchemyError as exc:
            if time.monotonic() >= deadline:
                log.error("database_unavailable", extra={"attempt": attempt, "error": str(exc)})
                raise
            log.warning("database_unavailable_retrying", extra={"attempt": attempt})
            time.sleep(interval)


def wait_for_schema(
    engine: Engine, timeout_seconds: float = 180.0, interval: float = 2.0
) -> None:
    """Block until migrations have been applied.

    Migrations run in the API pod's init container, so the worker and ingestor can start before the
    tables exist. Waiting here is what keeps them out of a crash loop on a first install.
    """
    wait_for_database(engine, timeout_seconds=timeout_seconds, interval=interval)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            with engine.connect() as connection:
                exists = connection.execute(
                    text("SELECT to_regclass('public.conversations')")
                ).scalar()
            if exists:
                log.info("schema_ready")
                return
        except SQLAlchemyError:
            exists = None
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for the conversations table to exist")
        log.warning("schema_not_ready_retrying")
        time.sleep(interval)
