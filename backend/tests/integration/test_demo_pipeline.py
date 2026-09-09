"""An injected failure must move the real pipeline, not just a flag.

These tests run the actual worker against a mocked SQS and a real PostgreSQL, with failures armed
through the same demo controls the UI uses. What they assert is that the job's recorded state,
attempt count and error type are indistinguishable from a genuine provider failure.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws
from sqlalchemy.orm import Session

from app import demo
from app.config import Settings
from app.demo import FaultInjectingProvider
from app.models import ErrorType, FailureMode, JobSource, JobStatus
from app.queue import SqsPublisher, SqsQueue
from app.repository import create_job, get_job
from app.schemas import ConversationInput
from app.scoring.provider import FakeProvider
from app.worker import ProcessOutcome, Worker
from tests.conftest import requires_postgres, sample_conversation

pytestmark = [requires_postgres, pytest.mark.integration]

QUEUE_NAME = "convoscore-demo-test"


@pytest.fixture
def sqs():
    with mock_aws():
        client = boto3.client("sqs", region_name="us-east-1")
        # Immediate redelivery so a retry can be observed without waiting out the real backoff.
        client.create_queue(QueueName=QUEUE_NAME, Attributes={"VisibilityTimeout": "0"})
        yield SqsQueue(client, QUEUE_NAME)


@pytest.fixture
def worker(sqs: SqsQueue, session: Session) -> Worker:
    settings = Settings(
        component="worker", demo_mode=True, max_attempts=3, worker_wait_time_seconds=0
    )
    provider = FaultInjectingProvider(FakeProvider(), lambda: session, timeout_seconds=0)
    instance = Worker(settings, sqs, provider, session_factory=lambda: session)
    # Collapse retry backoff; the delay is asserted separately in the worker tests.
    original = sqs.change_visibility
    sqs.change_visibility = lambda handle, seconds: original(handle, 0)  # type: ignore[method-assign]
    return instance


@pytest.fixture
def submit(session: Session, sqs: SqsQueue):
    def _submit():
        job = create_job(
            session,
            ConversationInput.model_validate(sample_conversation()),
            source=JobSource.API,
            max_attempts=3,
        )
        session.commit()
        SqsPublisher(sqs).publish(job.id)
        return job.id

    return _submit


def test_one_injected_failure_is_retried_and_then_succeeds(
    worker: Worker, submit, session: Session
) -> None:
    """The headline demo: a failure that the pipeline absorbs."""
    demo.arm(session, FailureMode.HTTP_500, 1)
    session.commit()
    job_id = submit()

    assert worker.run_once() is ProcessOutcome.RETRY_SCHEDULED
    after_failure = get_job(session, job_id)
    assert after_failure.status == JobStatus.PENDING.value
    assert after_failure.attempt_count == 1
    assert after_failure.last_error_type == ErrorType.SERVER_ERROR.value

    assert worker.run_once() is ProcessOutcome.COMPLETED
    completed = get_job(session, job_id)
    assert completed.status == JobStatus.COMPLETED.value
    assert completed.attempt_count == 2
    assert completed.risk_score is not None
    assert completed.last_error_type is None, "a success clears the earlier error"


def test_enough_injected_failures_exhaust_the_retries(
    worker: Worker, submit, session: Session
) -> None:
    """The second demo: a job the pipeline gives up on, with the reason recorded."""
    demo.arm(session, FailureMode.TIMEOUT, 4)
    session.commit()
    job_id = submit()

    assert worker.run_once() is ProcessOutcome.RETRY_SCHEDULED
    assert worker.run_once() is ProcessOutcome.RETRY_SCHEDULED
    assert worker.run_once() is ProcessOutcome.FAILED

    failed = get_job(session, job_id)
    assert failed.status == JobStatus.FAILED.value
    assert failed.attempt_count == 3
    assert failed.last_error_type == ErrorType.MAX_ATTEMPTS_EXHAUSTED.value
    assert failed.completed_at is not None
    # One token is left over, which is why the demo control caps and reports the count.
    assert demo.state(session).modes["timeout"] == 1


def test_malformed_response_fails_after_one_retry(
    worker: Worker, submit, session: Session
) -> None:
    """A model that answers in the wrong shape twice is a contract problem, not bad luck."""
    demo.arm(session, FailureMode.MALFORMED, 5)
    session.commit()
    job_id = submit()

    assert worker.run_once() is ProcessOutcome.RETRY_SCHEDULED
    assert worker.run_once() is ProcessOutcome.FAILED

    failed = get_job(session, job_id)
    assert failed.last_error_type == ErrorType.INVALID_LLM_RESPONSE.value
    assert failed.attempt_count == 2


def test_clearing_mid_demo_lets_the_job_through(
    worker: Worker, submit, session: Session
) -> None:
    """Recovering from the demo must be as easy as starting it."""
    demo.arm(session, FailureMode.HTTP_500, 10)
    session.commit()
    job_id = submit()

    assert worker.run_once() is ProcessOutcome.RETRY_SCHEDULED

    demo.clear(session)
    session.commit()

    assert worker.run_once() is ProcessOutcome.COMPLETED
    assert get_job(session, job_id).status == JobStatus.COMPLETED.value


def test_unarmed_jobs_are_unaffected(worker: Worker, submit, session: Session) -> None:
    job_id = submit()
    assert worker.run_once() is ProcessOutcome.COMPLETED
    assert get_job(session, job_id).attempt_count == 1
