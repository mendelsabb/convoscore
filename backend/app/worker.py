"""The scoring worker.

A long-running process that pulls job ids off SQS and scores them. Kubernetes runs a Deployment of
these; nothing creates a pod per conversation.

Two orderings matter, and both are load-bearing:

1. **The result is committed to PostgreSQL before the SQS message is deleted.** If the process dies
   in between, the message is redelivered and the job is found already complete, so the message is
   simply acknowledged. The reverse order would lose results whenever a worker died at the wrong
   moment.
2. **The job is claimed before the model is called, and a job that is already finished is never
   scored again.** At-least-once delivery means duplicates are normal, not exceptional; without
   this check every duplicate would be a second OpenAI charge for a result we already have.

The LLM call happens outside any database transaction. Holding one open across a multi-second
network call would pin a connection and lengthen every lock it holds.
"""

from __future__ import annotations

import contextlib
import random
import signal
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum, auto
from types import FrameType
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import metrics, metrics_server
from app.config import Settings, get_settings
from app.db import get_engine, get_session_factory, wait_for_schema
from app.factories import build_provider, build_queue
from app.heartbeat import touch as touch_heartbeat
from app.logging import configure_logging, get_logger
from app.models import ErrorType
from app.queue import QueueMessage, QueueUnavailableError, SqsQueue
from app.repository import (
    ClaimOutcome,
    claim_job,
    complete_job,
    fail_job,
    get_job,
    release_job_for_retry,
)
from app.scoring.provider import (
    LLMError,
    LLMInvalidResponseError,
    LLMPermanentError,
    LLMProvider,
    ScoringOutcome,
)

log = get_logger(__name__)


class ProcessOutcome(StrEnum):
    IDLE = auto()
    COMPLETED = auto()
    RETRY_SCHEDULED = auto()
    FAILED = auto()
    DUPLICATE_SKIPPED = auto()
    HELD_BY_ANOTHER_WORKER = auto()
    UNKNOWN_JOB = auto()
    MALFORMED_MESSAGE = auto()
    LOST_CLAIM = auto()


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    error_type: ErrorType
    backoff_seconds: int


def backoff_seconds(
    attempt: int, base: int, maximum: int, jitter: Callable[[], float] = random.random
) -> int:
    """Exponential backoff with jitter, in seconds.

    Jitter matters with several workers: without it, a provider outage would synchronise every
    retry into simultaneous bursts against a service that is already struggling.
    """
    capped = min(maximum, base * (2 ** max(0, attempt - 1)))
    return max(1, int(capped * (0.5 + 0.5 * jitter())))


def decide_retry(
    error: LLMError,
    attempt_count: int,
    max_attempts: int,
    base_backoff: int,
    max_backoff: int,
    jitter: Callable[[], float] = random.random,
) -> RetryDecision:
    """Decide what to do with a failed attempt.

    Permanent errors are never retried: a bad API key or a malformed request will fail identically
    every time, and retrying only delays the failure and wastes quota.

    An invalid response gets exactly one more chance. Once is plausibly a bad sample; twice means
    the prompt, the schema or the model is wrong, and more attempts will not fix it.
    """
    delay = backoff_seconds(attempt_count, base_backoff, max_backoff, jitter)

    if isinstance(error, LLMPermanentError):
        return RetryDecision(False, error.error_type, delay)

    if isinstance(error, LLMInvalidResponseError):
        retry = attempt_count < min(2, max_attempts)
        return RetryDecision(retry, error.error_type, delay)

    if attempt_count >= max_attempts:
        return RetryDecision(False, ErrorType.MAX_ATTEMPTS_EXHAUSTED, delay)
    return RetryDecision(True, error.error_type, delay)


class Worker:
    """Receives, claims, scores, persists, acknowledges. One message at a time."""

    def __init__(
        self,
        settings: Settings,
        queue: SqsQueue,
        provider: LLMProvider,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self.settings = settings
        self.queue = queue
        self.provider = provider
        self._session_factory = session_factory or (lambda: get_session_factory()())
        self._stop = False
        self.last_heartbeat = time.time()

    # -- lifecycle ---------------------------------------------------------------------

    def request_stop(self, signum: int | None = None, frame: FrameType | None = None) -> None:
        """Stop after the current message.

        Kubernetes sends SIGTERM on every rollout. Finishing the in-flight job and then exiting
        means a deploy does not manufacture retries and half-scored jobs.
        """
        log.info("shutdown_requested", extra={"signal": signum})
        self._stop = True

    @property
    def stopping(self) -> bool:
        return self._stop

    @contextlib.contextmanager
    def _session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- main loop ---------------------------------------------------------------------

    def run_forever(self) -> None:
        log.info(
            "worker_started",
            extra={"queue": self.queue.queue_name, "provider": self.provider.name},
        )
        consecutive_queue_errors = 0
        while not self._stop:
            try:
                self.run_once()
                consecutive_queue_errors = 0
            except QueueUnavailableError as exc:
                # The queue may genuinely be gone (LocalStack restarted). Back off, then re-resolve.
                consecutive_queue_errors += 1
                metrics.worker_receive_errors_total.labels(reason="queue_unavailable").inc()
                delay = min(30, 2**consecutive_queue_errors)
                log.warning(
                    "queue_unavailable",
                    extra={"error": str(exc), "backoff_seconds": delay,
                           "consecutive_errors": consecutive_queue_errors},
                )
                self.queue.forget_url()
                self._sleep(delay)
            except SQLAlchemyError as exc:
                # Leave the message unacknowledged: SQS will redeliver once the database is back.
                metrics.worker_receive_errors_total.labels(reason="database").inc()
                log.error("database_error", extra={"error": str(exc)})
                self._sleep(5)
            except Exception:
                metrics.worker_receive_errors_total.labels(reason="unexpected").inc()
                log.exception("worker_iteration_failed")
                self._sleep(5)
        log.info("worker_stopped")

    def _sleep(self, seconds: float) -> None:
        """Interruptible sleep, so SIGTERM does not have to wait out a long backoff."""
        deadline = time.monotonic() + seconds
        while not self._stop and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))

    def run_once(self) -> ProcessOutcome:
        """Handle at most one message. Returns IDLE when the queue is empty."""
        self.last_heartbeat = time.time()
        touch_heartbeat()
        metrics.worker_loop_iterations_total.inc()
        metrics.component_heartbeat_timestamp_seconds.labels(component="worker").set(
            self.last_heartbeat
        )
        messages = self.queue.receive(
            wait_time_seconds=self.settings.worker_wait_time_seconds, max_messages=1
        )
        if not messages:
            return ProcessOutcome.IDLE
        return self.process(messages[0])

    # -- one message -------------------------------------------------------------------

    def process(self, message: QueueMessage) -> ProcessOutcome:
        job_id = message.job_id
        if job_id is None:
            # Nothing we can ever do with this. Drop it rather than let it cycle to the DLQ.
            log.error("malformed_message_discarded", extra={"message_id": message.message_id})
            self.queue.delete(message.receipt_handle)
            return ProcessOutcome.MALFORMED_MESSAGE

        claim = self._claim(job_id)

        if claim is ClaimOutcome.NOT_FOUND:
            log.warning("job_not_found", extra={"job_id": str(job_id)})
            self.queue.delete(message.receipt_handle)
            return ProcessOutcome.UNKNOWN_JOB

        if claim is ClaimOutcome.ALREADY_FINISHED:
            # A duplicate delivery of work that is already done. Acknowledge it and, crucially,
            # do not call the model again.
            metrics.job_attempts_total.labels(outcome="duplicate").inc()
            log.info(
                "duplicate_delivery_skipped",
                extra={"job_id": str(job_id), "receive_count": message.receive_count},
            )
            self.queue.delete(message.receipt_handle)
            return ProcessOutcome.DUPLICATE_SKIPPED

        if claim is ClaimOutcome.HELD_BY_ANOTHER_WORKER:
            # Someone else is on it. Leave the message; SQS redelivers if they die.
            log.info("job_held_by_another_worker", extra={"job_id": str(job_id)})
            return ProcessOutcome.HELD_BY_ANOTHER_WORKER

        return self._score_and_persist(job_id, message)

    def _claim(self, job_id: uuid.UUID) -> ClaimOutcome:
        """Take the job in its own short transaction, so other workers see it immediately."""
        with self._session() as session:
            return claim_job(session, job_id, self.settings.visibility_timeout_seconds).outcome

    def _score_and_persist(self, job_id: uuid.UUID, message: QueueMessage) -> ProcessOutcome:
        with self._session() as session:
            job = get_job(session, job_id)
            if job is None:  # pragma: no cover - it was claimed a moment ago
                self.queue.delete(message.receipt_handle)
                return ProcessOutcome.UNKNOWN_JOB
            conversation: dict[str, Any] = job.conversation
            attempt = job.attempt_count
            source = job.source

        started = time.monotonic()
        try:
            outcome = self.provider.score(conversation)
        except LLMError as error:
            return self._handle_failure(job_id, message, error, attempt, source)

        duration_ms = int((time.monotonic() - started) * 1000)
        self._record_success_metrics(outcome)

        # Persist the result BEFORE acknowledging the message. If the process dies here, the
        # message reappears, the job is found completed, and it is acknowledged without re-scoring.
        with self._session() as session:
            persisted = complete_job(
                session,
                job_id,
                sentiment=outcome.result.sentiment,
                risk_score=outcome.result.risk_score,
                rationale=outcome.result.rationale,
                model=outcome.model,
                prompt_version=outcome.prompt_version,
                schema_version=outcome.schema_version,
                prompt_tokens=outcome.prompt_tokens,
                completion_tokens=outcome.completion_tokens,
                total_tokens=outcome.total_tokens,
                llm_latency_ms=outcome.latency_ms,
                estimated_cost_usd=outcome.estimated_cost_usd,
                pricing_version=outcome.pricing_version,
            )

        if not persisted:
            # Our claim expired and another worker finished it. Its result stands; ours is dropped.
            metrics.job_attempts_total.labels(outcome="lost_claim").inc()
            log.warning("claim_lost_result_discarded", extra={"job_id": str(job_id)})
            self.queue.delete(message.receipt_handle)
            return ProcessOutcome.LOST_CLAIM

        self.queue.delete(message.receipt_handle)
        metrics.jobs_completed_total.labels(source=source).inc()
        metrics.job_attempts_total.labels(outcome="completed").inc()
        metrics.job_processing_duration_seconds.observe(duration_ms / 1000.0)
        log.info(
            "job_completed",
            extra={
                "job_id": str(job_id),
                "attempt": attempt,
                "risk_score": outcome.result.risk_score,
                "sentiment": outcome.result.sentiment,
                "model": outcome.model,
                "llm_latency_ms": outcome.latency_ms,
                "total_tokens": outcome.total_tokens,
                "duration_ms": duration_ms,
            },
        )
        return ProcessOutcome.COMPLETED

    def _record_success_metrics(self, outcome: ScoringOutcome) -> None:
        """Provider-level signals for a successful call: latency, tokens and estimated spend."""
        model = outcome.model
        metrics.llm_calls_total.labels(model=model, outcome="success").inc()
        metrics.llm_latency_seconds.labels(model=model).observe(outcome.latency_ms / 1000.0)

        if outcome.prompt_tokens is not None:
            metrics.llm_tokens_total.labels(model=model, kind="prompt").inc(outcome.prompt_tokens)
        if outcome.completion_tokens is not None:
            metrics.llm_tokens_total.labels(model=model, kind="completion").inc(
                outcome.completion_tokens
            )

        if outcome.estimated_cost_usd is not None:
            metrics.llm_estimated_cost_usd_total.labels(model=model).inc(
                float(outcome.estimated_cost_usd)
            )
        else:
            # A model missing from the price table would otherwise make spend silently
            # under-report, which is worse than a visible gap.
            metrics.llm_pricing_unknown_total.labels(model=model).inc()

    def _handle_failure(
        self,
        job_id: uuid.UUID,
        message: QueueMessage,
        error: LLMError,
        attempt: int,
        source: str,
    ) -> ProcessOutcome:
        # The provider call failed: record what kind of failure it was before deciding what to do
        # about it, so the error mix is visible even for errors that are retried successfully.
        metrics.llm_calls_total.labels(
            model=self.settings.openai_model, outcome=error.error_type.value
        ).inc()
        if isinstance(error, LLMInvalidResponseError):
            metrics.llm_validation_failures_total.inc()

        decision = decide_retry(
            error,
            attempt_count=attempt,
            max_attempts=self.settings.max_attempts,
            base_backoff=self.settings.retry_base_backoff_seconds,
            max_backoff=self.settings.retry_max_backoff_seconds,
        )

        if decision.retry:
            with self._session() as session:
                release_job_for_retry(session, job_id, decision.error_type, str(error))
            # Extend visibility instead of deleting: the retry is owned by SQS, so it happens even
            # if this worker dies right now.
            self.queue.change_visibility(message.receipt_handle, decision.backoff_seconds)
            metrics.llm_retries_total.labels(error_type=decision.error_type.value).inc()
            metrics.job_attempts_total.labels(outcome="retried").inc()
            log.warning(
                "job_attempt_failed_retrying",
                extra={
                    "job_id": str(job_id),
                    "attempt": attempt,
                    "max_attempts": self.settings.max_attempts,
                    "error_type": decision.error_type.value,
                    "backoff_seconds": decision.backoff_seconds,
                },
            )
            return ProcessOutcome.RETRY_SCHEDULED

        with self._session() as session:
            fail_job(session, job_id, decision.error_type, str(error))
        self.queue.delete(message.receipt_handle)
        metrics.jobs_failed_total.labels(source=source, error_type=decision.error_type.value).inc()
        metrics.job_attempts_total.labels(outcome="failed").inc()
        log.error(
            "job_failed",
            extra={
                "job_id": str(job_id),
                "attempt": attempt,
                "error_type": decision.error_type.value,
            },
        )
        return ProcessOutcome.FAILED


def install_queue_gauges(queue: SqsQueue, dlq_name: str) -> None:
    """Publish queue depth as scrape-time gauges.

    Depth is a state, not an event, so it is read when Prometheus asks rather than pushed when
    something happens: a stalled worker would otherwise leave the graph frozen at its last value.

    Both worker replicas report the same numbers, so the dashboard aggregates with max() rather
    than sum(). Attributes are cached briefly so a scrape does not mean an SQS call per replica
    per second.
    """
    cache: dict[str, tuple[float, dict[str, int]]] = {}

    def depths() -> dict[tuple[str, str], int]:
        import time as _time

        result: dict[tuple[str, str], int] = {}
        for label, name in (("main", queue.queue_name), ("dlq", dlq_name)):
            cached = cache.get(label)
            if cached and _time.monotonic() - cached[0] < 10:
                counts = cached[1]
            else:
                probe = queue if label == "main" else queue.sibling(name)
                counts = probe.attributes()
                cache[label] = (_time.monotonic(), counts)
            result[(label, "visible")] = counts["visible"]
            result[(label, "in_flight")] = counts["in_flight"]
        return result

    gauges = metrics.CallbackGauges()
    gauges.register(
        "convoscore_queue_messages",
        "Messages on the scoring queue and its dead-letter queue.",
        ["queue", "state"],
        depths,
    )
    from prometheus_client import REGISTRY

    REGISTRY.register(gauges)


def main() -> int:
    settings = get_settings()
    configure_logging("worker", settings.log_level, settings.log_json)

    # Migrations run in the API pod's init container, so the schema may not exist yet on a first
    # install. Waiting keeps this pod out of a crash loop.
    wait_for_schema(get_engine())

    queue = build_queue(settings)
    install_queue_gauges(queue, settings.sqs_dlq_name)
    metrics_server.start(settings.metrics_port, component="worker")

    provider = build_provider(settings)
    if settings.demo_mode:
        # A pass-through wrapper unless a failure has been armed, so leaving it installed costs
        # nothing. What it cannot do is fake a metric: it makes the real call fail instead.
        from app.demo import FaultInjectingProvider

        provider = FaultInjectingProvider(provider, lambda: get_session_factory()())
        log.warning("demo_mode_enabled", extra={"detail": "failure injection is available"})

    worker = Worker(settings, queue, provider)
    signal.signal(signal.SIGTERM, worker.request_stop)
    signal.signal(signal.SIGINT, worker.request_stop)
    worker.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
