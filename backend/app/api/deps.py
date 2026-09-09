"""Request-scoped dependencies."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_session_factory
from app.queue import JobPublisher


def get_db() -> Iterator[Session]:
    """One transaction per request: commit on success, roll back on failure."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_publisher(request: Request) -> JobPublisher:
    """The queue publisher chosen at startup and stored on the app state."""
    return request.app.state.publisher


DbSession = Annotated[Session, Depends(get_db)]
Publisher = Annotated[JobPublisher, Depends(get_publisher)]
AppSettings = Annotated[Settings, Depends(get_settings)]
