"""The job queue: an SQS-compatible port plus test doubles.

Only job ids travel through the queue. The conversation, its result and its state live in
PostgreSQL, so a lost or duplicated message costs a redelivery, never data.

The queue URL is resolved from the queue *name* at first use and re-resolved if it goes away.
LocalStack does not persist state at the tier we use, so a LocalStack restart during a demo would
otherwise wedge the workers permanently.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from app.logging import get_logger

log = get_logger(__name__)


class QueuePublishError(RuntimeError):
    """A job id could not be enqueued.

    The caller records the job as failed with ``enqueue_failed`` and returns 503, which makes the
    dual-write gap visible instead of leaving a row nothing will ever process.
    """


class QueueUnavailableError(RuntimeError):
    """The queue could not be reached or does not exist. The worker backs off and re-resolves."""


@dataclass(frozen=True)
class QueueMessage:
    message_id: str
    receipt_handle: str
    body: dict[str, Any]
    receive_count: int

    @property
    def job_id(self) -> uuid.UUID | None:
        """The job id, or None if the body is not something we put there."""
        raw = self.body.get("job_id")
        if not isinstance(raw, str):
            return None
        try:
            return uuid.UUID(raw)
        except ValueError:
            return None


class JobPublisher(Protocol):
    def publish(self, job_id: uuid.UUID) -> None:
        """Enqueue a job id, or raise QueuePublishError."""
        ...


# --------------------------------------------------------------------------------------
# SQS
# --------------------------------------------------------------------------------------


def build_sqs_client(
    endpoint_url: str | None,
    region: str,
    access_key_id: str | None = None,
    secret_access_key: str | None = None,
) -> Any:
    """Create a boto3 SQS client.

    With no explicit credentials boto3 uses its normal chain, which in production is the pod's IAM
    role. Locally, LocalStack accepts the conventional dummy pair.
    """
    import boto3
    from botocore.config import Config

    return boto3.client(
        "sqs",
        endpoint_url=endpoint_url,
        region_name=region,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=Config(
            retries={"max_attempts": 3, "mode": "standard"},
            # Must exceed the long-poll wait or every receive would time out client-side.
            read_timeout=35,
            connect_timeout=5,
        ),
    )


class SqsQueue:
    """Thin wrapper over one SQS queue, resolved by name."""

    def __init__(self, client: Any, queue_name: str) -> None:
        self._client = client
        self.queue_name = queue_name
        self._queue_url: str | None = None

    # -- url resolution ----------------------------------------------------------------

    def queue_url(self) -> str:
        if self._queue_url is None:
            self._queue_url = self._resolve_url()
        return self._queue_url

    def forget_url(self) -> None:
        """Drop the cached URL so the next call re-resolves it.

        Needed after a LocalStack restart or a recreated queue.
        """
        self._queue_url = None

    def _resolve_url(self) -> str:
        try:
            return self._client.get_queue_url(QueueName=self.queue_name)["QueueUrl"]
        except Exception as exc:
            raise QueueUnavailableError(
                f"could not resolve queue {self.queue_name!r}: {exc}"
            ) from exc

    # -- operations --------------------------------------------------------------------

    def send(self, job_id: uuid.UUID) -> str:
        try:
            response = self._client.send_message(
                QueueUrl=self.queue_url(),
                MessageBody=json.dumps({"job_id": str(job_id)}),
            )
        except Exception as exc:
            self.forget_url()
            raise QueuePublishError(f"could not enqueue job: {exc}") from exc
        return response["MessageId"]

    def receive(self, wait_time_seconds: int = 20, max_messages: int = 1) -> list[QueueMessage]:
        """Long-poll for messages. An empty list simply means nothing is waiting."""
        try:
            response = self._client.receive_message(
                QueueUrl=self.queue_url(),
                MaxNumberOfMessages=max_messages,
                WaitTimeSeconds=wait_time_seconds,
                AttributeNames=["ApproximateReceiveCount"],
            )
        except Exception as exc:
            self.forget_url()
            raise QueueUnavailableError(f"receive failed: {exc}") from exc

        messages = []
        for raw in response.get("Messages", []):
            try:
                body = json.loads(raw["Body"])
            except (ValueError, TypeError):
                body = {}
            messages.append(
                QueueMessage(
                    message_id=raw.get("MessageId", ""),
                    receipt_handle=raw["ReceiptHandle"],
                    body=body if isinstance(body, dict) else {},
                    receive_count=int(
                        raw.get("Attributes", {}).get("ApproximateReceiveCount", 1)
                    ),
                )
            )
        return messages

    def delete(self, receipt_handle: str) -> None:
        """Acknowledge a message. Only ever called once the outcome is durable in PostgreSQL."""
        try:
            self._client.delete_message(QueueUrl=self.queue_url(), ReceiptHandle=receipt_handle)
        except Exception as exc:
            self.forget_url()
            raise QueueUnavailableError(f"delete failed: {exc}") from exc

    def change_visibility(self, receipt_handle: str, seconds: int) -> None:
        """Schedule the next redelivery. This is how retry backoff is applied.

        Failure here is not fatal: the message simply becomes visible again when its original
        timeout expires, so the job is still retried, just sooner than intended.
        """
        try:
            self._client.change_message_visibility(
                QueueUrl=self.queue_url(),
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=seconds,
            )
        except Exception as exc:
            log.warning("change_visibility_failed", extra={"error": str(exc)})

    def attributes(self) -> dict[str, int]:
        """Queue depth signals for metrics and dashboards."""
        try:
            response = self._client.get_queue_attributes(
                QueueUrl=self.queue_url(),
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                ],
            )
        except Exception as exc:
            self.forget_url()
            raise QueueUnavailableError(f"get_queue_attributes failed: {exc}") from exc
        attributes = response.get("Attributes", {})
        return {
            "visible": int(attributes.get("ApproximateNumberOfMessages", 0)),
            "in_flight": int(attributes.get("ApproximateNumberOfMessagesNotVisible", 0)),
        }


class SqsPublisher:
    """Publishes job ids to an SQS queue."""

    def __init__(self, queue: SqsQueue) -> None:
        self.queue = queue

    def publish(self, job_id: uuid.UUID) -> None:
        message_id = self.queue.send(job_id)
        log.debug("job_enqueued", extra={"job_id": str(job_id), "message_id": message_id})


# --------------------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------------------


class InMemoryPublisher:
    """Records published ids without a broker."""

    def __init__(self) -> None:
        self.published: list[uuid.UUID] = []

    def publish(self, job_id: uuid.UUID) -> None:
        self.published.append(job_id)


class FailingPublisher:
    """Always raises. Used to exercise the enqueue-failure path."""

    def __init__(self, message: str = "queue unavailable") -> None:
        self.message = message

    def publish(self, job_id: uuid.UUID) -> None:
        raise QueuePublishError(self.message)
