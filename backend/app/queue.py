"""The job queue port.

The SQS implementation arrives with the worker (milestone 3). Defining the port here keeps job
creation testable and means the API never imports boto3 directly.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from app.logging import get_logger

log = get_logger(__name__)


class QueuePublishError(RuntimeError):
    """Raised when a job id could not be enqueued.

    The caller records the job as failed with ``enqueue_failed`` and returns 503, which makes the
    dual-write gap visible instead of leaving a row nothing will ever process.
    """


class JobPublisher(Protocol):
    def publish(self, job_id: uuid.UUID) -> None:
        """Enqueue a job id, or raise QueuePublishError."""
        ...


class InMemoryPublisher:
    """Records published ids without a broker. Used by tests and by the API before SQS is wired."""

    def __init__(self) -> None:
        self.published: list[uuid.UUID] = []

    def publish(self, job_id: uuid.UUID) -> None:
        self.published.append(job_id)
        log.debug("job_published_in_memory", extra={"job_id": str(job_id)})


class FailingPublisher:
    """Always raises. Used to exercise the enqueue-failure path in tests."""

    def __init__(self, message: str = "queue unavailable") -> None:
        self.message = message

    def publish(self, job_id: uuid.UUID) -> None:
        raise QueuePublishError(self.message)
