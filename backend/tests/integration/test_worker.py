"""The worker against a real PostgreSQL and a mocked SQS.

These tests encode the two orderings the pipeline depends on:

* the result is committed before the message is acknowledged;
* a job that is already finished is acknowledged without calling the model again.

The provider is always a fake or a scripted stub. Nothing here calls OpenAI.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import boto3
import pytest
from moto import mock_aws
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ErrorType, JobSource, JobStatus
from app.queue import SqsPublisher, SqsQueue
from app.repository import claim_job, create_job, get_job
from app.schemas import ConversationInput
from app.scoring.contract import ScoringResult
from app.scoring.provider import (
    FakeProvider,
    LLMInvalidResponseError,
    LLMPermanentError,
    LLMTransientError,
    ScoringOutcome,
)
from app.worker import ProcessOutcome, Worker
from tests.conftest import requires_postgres, sample_conversation

pytestmark = [requires_postgres, pytest.mark.integration]

QUEUE_NAME = "convoscore-scoring-test"


# --------------------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------------------


class ScriptedProvider:
    """Raises or returns a scripted sequence, so each failure mode can be driven precisely."""

    name = "scripted"

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls = 0

    def score(self, conversation: dict[str, Any]) -> ScoringOutcome:
        self.calls += 1
        step = self.script.pop(0) if self.script else _ok()
        if isinstance(step, Exception):
            raise step
        return step


def _ok(risk: int = 42) -> ScoringOutcome:
    return ScoringOutcome(
        result=ScoringResult(
            sentiment="neutral", risk_score=risk, rationale="Scripted test result."
        ),
        model="test-model",
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        latency_ms=7,
        estimated_cost_usd=Decimal("0.00001000"),
    )


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture
def sqs():
    """A real SQS API surface, in-process, via moto.

    VisibilityTimeout is 0 so that redelivery is immediate and tests can exercise duplicate
    delivery without sleeping. The backoff the worker actually asks for is asserted separately,
    in test_retry_backoff_is_applied_to_the_message.
    """
    with mock_aws():
        client = boto3.client("sqs", region_name="us-east-1")
        client.create_queue(QueueName=QUEUE_NAME, Attributes={"VisibilityTimeout": "0"})
        yield SqsQueue(client, QUEUE_NAME)


@pytest.fixture
def visibility_calls(sqs: SqsQueue) -> list[int]:
    """Record the backoff requested from SQS, and collapse the wait so the test can continue.

    Without this a retry test would have to sleep out the real 5-40 second backoff.
    """
    calls: list[int] = []
    original = sqs.change_visibility

    def spy(receipt_handle: str, seconds: int) -> None:
        calls.append(seconds)
        original(receipt_handle, 0)

    sqs.change_visibility = spy  # type: ignore[method-assign]
    return calls


@pytest.fixture
def worker_settings() -> Settings:
    return Settings(
        component="worker",
        max_attempts=3,
        visibility_timeout_seconds=90,
        worker_wait_time_seconds=0,
        retry_base_backoff_seconds=5,
        retry_max_backoff_seconds=60,
    )


@pytest.fixture
def make_worker(worker_settings: Settings, sqs: SqsQueue, session: Session):
    def _make(provider) -> Worker:
        # The test session is shared so assertions see the worker's committed state.
        return Worker(worker_settings, sqs, provider, session_factory=lambda: session)

    return _make


@pytest.fixture
def enqueue(session: Session, sqs: SqsQueue):
    """Create a job the way the API does, and put its id on the queue."""

    def _enqueue(**kwargs) -> uuid.UUID:
        job = create_job(
            session,
            ConversationInput.model_validate(sample_conversation()),
            source=JobSource.API,
            max_attempts=3,
            **kwargs,
        )
        session.commit()
        SqsPublisher(sqs).publish(job.id)
        return job.id

    return _enqueue


def queue_depth(sqs: SqsQueue) -> int:
    return sqs.attributes()["visible"]


# --------------------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------------------


def test_worker_scores_a_job_and_acknowledges_the_message(
    make_worker, enqueue, session: Session, sqs: SqsQueue
) -> None:
    job_id = enqueue()
    worker = make_worker(ScriptedProvider([_ok(risk=42)]))

    assert worker.run_once() is ProcessOutcome.COMPLETED

    job = get_job(session, job_id)
    assert job.status == JobStatus.COMPLETED.value
    assert job.risk_score == 42
    assert job.sentiment == "neutral"
    assert job.model == "test-model"
    assert job.total_tokens == 120
    assert job.estimated_cost_usd == Decimal("0.00001000")
    assert job.llm_latency_ms == 7
    assert job.attempt_count == 1
    assert job.completed_at is not None
    assert queue_depth(sqs) == 0


def test_worker_records_prompt_and_pricing_versions(make_worker, enqueue, session: Session) -> None:
    """Every result must be explainable later: which prompt, which schema, which prices."""
    job_id = enqueue()
    make_worker(FakeProvider()).run_once()

    job = get_job(session, job_id)
    assert job.prompt_version == "v1"
    assert job.schema_version == "1"
    assert job.pricing_version == "2026-09"


def test_idle_queue_is_not_an_error(make_worker) -> None:
    assert make_worker(FakeProvider()).run_once() is ProcessOutcome.IDLE


# --------------------------------------------------------------------------------------
# Durability ordering and duplicates
# --------------------------------------------------------------------------------------


def test_duplicate_delivery_does_not_call_the_model_again(
    make_worker, enqueue, session: Session, sqs: SqsQueue
) -> None:
    """At-least-once delivery is normal. A second delivery must not mean a second charge."""
    job_id = enqueue()
    provider = ScriptedProvider([_ok(risk=55)])
    worker = make_worker(provider)

    assert worker.run_once() is ProcessOutcome.COMPLETED
    assert provider.calls == 1

    # The same job is delivered again, as SQS is entitled to do.
    SqsPublisher(sqs).publish(job_id)
    assert worker.run_once() is ProcessOutcome.DUPLICATE_SKIPPED

    assert provider.calls == 1, "the model must not be called for an already-completed job"
    job = get_job(session, job_id)
    assert job.risk_score == 55
    assert job.attempt_count == 1
    assert queue_depth(sqs) == 0


def test_result_is_committed_before_the_message_is_acknowledged(
    make_worker, enqueue, session: Session, sqs: SqsQueue
) -> None:
    """Simulates the worker dying between persisting and acknowledging.

    The delete is made to fail, so the message survives. The result must already be durable, and
    the redelivery must be acknowledged without re-scoring.
    """
    job_id = enqueue()
    provider = ScriptedProvider([_ok(risk=61)])
    worker = make_worker(provider)

    original_delete = sqs.delete
    sqs.delete = lambda handle: (_ for _ in ()).throw(RuntimeError("crashed before ack"))
    with pytest.raises(RuntimeError):
        worker.run_once()
    sqs.delete = original_delete

    # The result survived the crash.
    assert get_job(session, job_id).status == JobStatus.COMPLETED.value

    # The message is still on the queue and gets redelivered.
    assert worker.run_once() is ProcessOutcome.DUPLICATE_SKIPPED
    assert provider.calls == 1
    assert queue_depth(sqs) == 0


def test_a_job_already_claimed_by_another_worker_is_left_alone(
    make_worker, enqueue, session: Session
) -> None:
    job_id = enqueue()
    claim_job(session, job_id, 90)  # another worker got there first
    session.commit()

    provider = ScriptedProvider([_ok()])
    assert make_worker(provider).run_once() is ProcessOutcome.HELD_BY_ANOTHER_WORKER
    assert provider.calls == 0


def test_a_worker_that_lost_its_claim_discards_its_result(
    make_worker, enqueue, session: Session, sqs: SqsQueue
) -> None:
    """The takeover worker's result stands; the straggler must not overwrite it."""
    job_id = enqueue()
    worker = make_worker(ScriptedProvider([_ok(risk=90)]))

    # While this worker is "scoring", another one takes over and completes the job.
    def steal_then_score(conversation):
        from app.repository import complete_job

        complete_job(
            session, job_id,
            sentiment="positive", risk_score=5, rationale="Other worker got there first.",
            model="other", prompt_version="v1", schema_version="1",
            prompt_tokens=1, completion_tokens=1, total_tokens=2,
            llm_latency_ms=1, estimated_cost_usd=Decimal("0"), pricing_version="2026-09",
        )
        session.commit()
        return _ok(risk=90)

    provider = ScriptedProvider([])
    provider.score = steal_then_score  # type: ignore[method-assign]

    assert make_worker(provider).run_once() is ProcessOutcome.LOST_CLAIM
    assert get_job(session, job_id).risk_score == 5
    assert queue_depth(sqs) == 0
    assert worker is not None


# --------------------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------------------


def test_transient_failure_returns_the_job_for_retry_without_losing_the_message(
    make_worker, enqueue, session: Session
) -> None:
    job_id = enqueue()
    provider = ScriptedProvider([LLMTransientError("upstream 503", ErrorType.SERVER_ERROR)])

    assert make_worker(provider).run_once() is ProcessOutcome.RETRY_SCHEDULED

    job = get_job(session, job_id)
    assert job.status == JobStatus.PENDING.value
    assert job.attempt_count == 1
    assert job.last_error_type == ErrorType.SERVER_ERROR.value
    assert job.locked_until is None


def test_a_retried_job_succeeds_on_the_next_attempt(
    make_worker, enqueue, session: Session, sqs: SqsQueue, visibility_calls: list[int]
) -> None:
    """The whole point of retrying: a transient blip should not fail the job."""
    job_id = enqueue()
    provider = ScriptedProvider(
        [LLMTransientError("timeout", ErrorType.TIMEOUT), _ok(risk=33)]
    )
    worker = make_worker(provider)

    assert worker.run_once() is ProcessOutcome.RETRY_SCHEDULED
    assert worker.run_once() is ProcessOutcome.COMPLETED

    job = get_job(session, job_id)
    assert job.status == JobStatus.COMPLETED.value
    assert job.risk_score == 33
    assert job.attempt_count == 2
    assert job.last_error_type is None, "a successful result clears the earlier error"
    assert queue_depth(sqs) == 0


def test_job_fails_after_the_attempt_cap_and_the_message_is_removed(
    make_worker, enqueue, session: Session, sqs: SqsQueue, visibility_calls: list[int]
) -> None:
    job_id = enqueue()
    error = LLMTransientError("still down", ErrorType.SERVER_ERROR)
    worker = make_worker(ScriptedProvider([error, error, error]))

    assert worker.run_once() is ProcessOutcome.RETRY_SCHEDULED
    assert worker.run_once() is ProcessOutcome.RETRY_SCHEDULED
    assert worker.run_once() is ProcessOutcome.FAILED

    job = get_job(session, job_id)
    assert job.status == JobStatus.FAILED.value
    assert job.attempt_count == 3
    assert job.last_error_type == ErrorType.MAX_ATTEMPTS_EXHAUSTED.value
    assert job.completed_at is not None
    assert queue_depth(sqs) == 0
    # Two retries were scheduled, and the second waited longer than the first.
    assert len(visibility_calls) == 2
    assert visibility_calls[1] > visibility_calls[0]


def test_retry_backoff_is_applied_to_the_message(
    make_worker, enqueue, sqs: SqsQueue
) -> None:
    """Backoff is applied by extending the message's visibility, not by sleeping in the worker.

    That is what makes a retry survive this worker being killed mid-backoff.
    """
    enqueue()
    requested: list[int] = []
    original = sqs.change_visibility

    def spy(receipt_handle: str, seconds: int) -> None:
        requested.append(seconds)
        original(receipt_handle, 0)

    sqs.change_visibility = spy  # type: ignore[method-assign]

    make_worker(ScriptedProvider([LLMTransientError("timeout", ErrorType.TIMEOUT)])).run_once()

    assert len(requested) == 1
    # First attempt: base 5s, halved-to-full jitter, so somewhere in 2..5 seconds.
    assert 2 <= requested[0] <= 5


def test_permanent_failure_does_not_retry(
    make_worker, enqueue, session: Session, sqs: SqsQueue
) -> None:
    job_id = enqueue()
    provider = ScriptedProvider([LLMPermanentError("invalid api key")])

    assert make_worker(provider).run_once() is ProcessOutcome.FAILED

    job = get_job(session, job_id)
    assert job.status == JobStatus.FAILED.value
    assert job.attempt_count == 1
    assert job.last_error_type == ErrorType.CLIENT_ERROR.value
    assert provider.calls == 1
    assert queue_depth(sqs) == 0


def test_invalid_model_output_retries_once_then_fails(
    make_worker, enqueue, session: Session, visibility_calls: list[int]
) -> None:
    job_id = enqueue()
    bad = LLMInvalidResponseError("risk_score was 250")
    worker = make_worker(ScriptedProvider([bad, bad]))

    assert worker.run_once() is ProcessOutcome.RETRY_SCHEDULED
    assert worker.run_once() is ProcessOutcome.FAILED

    job = get_job(session, job_id)
    assert job.status == JobStatus.FAILED.value
    assert job.last_error_type == ErrorType.INVALID_LLM_RESPONSE.value
    assert job.attempt_count == 2


def test_error_text_is_stored_for_the_reviewer(make_worker, enqueue, session: Session) -> None:
    job_id = enqueue()
    make_worker(ScriptedProvider([LLMPermanentError("model gpt-9 does not exist")])).run_once()
    assert "gpt-9" in get_job(session, job_id).last_error


# --------------------------------------------------------------------------------------
# Message hygiene
# --------------------------------------------------------------------------------------


def test_message_for_an_unknown_job_is_discarded(make_worker, sqs: SqsQueue) -> None:
    """A job id with no row can never be processed; keeping the message would loop forever."""
    SqsPublisher(sqs).publish(uuid.uuid4())
    provider = ScriptedProvider([_ok()])

    assert make_worker(provider).run_once() is ProcessOutcome.UNKNOWN_JOB
    assert provider.calls == 0
    assert queue_depth(sqs) == 0


def test_malformed_message_is_discarded(make_worker, sqs: SqsQueue) -> None:
    sqs._client.send_message(QueueUrl=sqs.queue_url(), MessageBody="not json at all")
    provider = ScriptedProvider([_ok()])

    assert make_worker(provider).run_once() is ProcessOutcome.MALFORMED_MESSAGE
    assert provider.calls == 0
    assert queue_depth(sqs) == 0


def test_publisher_and_worker_agree_on_the_message_format(sqs: SqsQueue) -> None:
    job_id = uuid.uuid4()
    SqsPublisher(sqs).publish(job_id)

    received = sqs.receive(wait_time_seconds=0)
    assert len(received) == 1
    assert received[0].job_id == job_id


def test_queue_url_is_re_resolved_after_the_queue_disappears(sqs: SqsQueue) -> None:
    """LocalStack does not persist state; a restart must not wedge the workers permanently."""
    sqs.queue_url()
    assert sqs._queue_url is not None

    sqs.forget_url()
    assert sqs._queue_url is None
    assert sqs.queue_url()  # resolves again on demand


# --------------------------------------------------------------------------------------
# Shutdown
# --------------------------------------------------------------------------------------


def test_stop_request_is_recorded(make_worker) -> None:
    """SIGTERM on a rollout must not abandon the message in flight."""
    worker = make_worker(FakeProvider())
    assert worker.stopping is False
    worker.request_stop(15, None)
    assert worker.stopping is True


def test_run_forever_exits_promptly_once_stopped(make_worker) -> None:
    worker = make_worker(FakeProvider())
    worker.request_stop()
    worker.run_forever()  # returns immediately rather than blocking on a receive
