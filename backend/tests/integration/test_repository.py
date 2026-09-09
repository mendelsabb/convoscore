"""Job state transitions against a real PostgreSQL.

These tests encode the reliability contract: at-least-once delivery must not produce duplicate
scoring, a dead worker's claim must expire, and a worker that lost its claim must not overwrite
the result of the worker that took over.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Conversation, ErrorType, JobSource, JobStatus
from app.repository import (
    ClaimOutcome,
    claim_job,
    complete_job,
    count_by_status,
    create_job,
    fail_job,
    get_job,
    list_conversations,
    mark_enqueue_failed,
    mark_enqueued,
    oldest_pending_age_seconds,
    release_job_for_retry,
    stats,
)
from app.schemas import ConversationInput
from tests.conftest import requires_postgres, sample_conversation

pytestmark = [requires_postgres, pytest.mark.integration]

VISIBILITY = 90


def make_job(session: Session, source: JobSource = JobSource.API, **kwargs) -> Conversation:
    conversation = ConversationInput.model_validate(sample_conversation())
    job = create_job(session, conversation, source=source, max_attempts=3, **kwargs)
    session.commit()
    return job


def expire_claim(session: Session, job_id: uuid.UUID) -> None:
    """Simulate a worker that died: its claim ages out."""
    session.execute(
        text("UPDATE conversations SET locked_until = now() - interval '1 second' WHERE id = :id"),
        {"id": job_id},
    )
    session.commit()


def complete_with_score(session: Session, job_id: uuid.UUID, risk: int = 70) -> bool:
    return complete_job(
        session,
        job_id,
        sentiment="negative",
        risk_score=risk,
        rationale="Customer chased three times.",
        model="gpt-4.1-mini",
        prompt_version="v1",
        schema_version="1",
        prompt_tokens=400,
        completion_tokens=60,
        total_tokens=460,
        llm_latency_ms=1200,
        estimated_cost_usd=Decimal("0.00025600"),
        pricing_version="2026-09",
    )


def test_create_job_starts_pending_with_no_attempts(session: Session) -> None:
    job = make_job(session)
    assert job.status == JobStatus.PENDING.value
    assert job.attempt_count == 0
    assert job.message_count == 3
    assert len(job.conversation_hash) == 64
    assert job.enqueued_at is None


def test_mark_enqueued_records_the_handoff(session: Session) -> None:
    job = make_job(session)
    mark_enqueued(session, job.id)
    session.commit()
    assert get_job(session, job.id).enqueued_at is not None


def test_claim_marks_processing_and_counts_the_attempt(session: Session) -> None:
    job = make_job(session)
    result = claim_job(session, job.id, VISIBILITY)
    session.commit()

    assert result.outcome is ClaimOutcome.CLAIMED
    assert result.job is not None
    assert result.job.status == JobStatus.PROCESSING.value
    assert result.job.attempt_count == 1
    assert result.job.started_at is not None
    assert result.job.locked_until > datetime.now(UTC)


def test_second_worker_cannot_claim_a_live_claim(session: Session) -> None:
    """Two workers handed the same message: only one may score it."""
    job = make_job(session)
    first = claim_job(session, job.id, VISIBILITY)
    session.commit()
    second = claim_job(session, job.id, VISIBILITY)
    session.commit()

    assert first.outcome is ClaimOutcome.CLAIMED
    assert second.outcome is ClaimOutcome.HELD_BY_ANOTHER_WORKER
    assert get_job(session, job.id).attempt_count == 1


def test_expired_claim_can_be_taken_over(session: Session) -> None:
    """A worker that crashed mid-call must not strand the job forever."""
    job = make_job(session)
    claim_job(session, job.id, VISIBILITY)
    session.commit()
    expire_claim(session, job.id)

    retaken = claim_job(session, job.id, VISIBILITY)
    session.commit()

    assert retaken.outcome is ClaimOutcome.CLAIMED
    assert retaken.job.attempt_count == 2


def test_duplicate_delivery_of_a_finished_job_does_no_work(session: Session) -> None:
    """The case that would otherwise bill us twice for the same conversation."""
    job = make_job(session)
    claim_job(session, job.id, VISIBILITY)
    complete_with_score(session, job.id)
    session.commit()

    redelivered = claim_job(session, job.id, VISIBILITY)
    session.commit()

    assert redelivered.outcome is ClaimOutcome.ALREADY_FINISHED
    assert get_job(session, job.id).attempt_count == 1


def test_claim_reports_missing_job(session: Session) -> None:
    assert claim_job(session, uuid.uuid4(), VISIBILITY).outcome is ClaimOutcome.NOT_FOUND


def test_complete_persists_result_and_clears_the_claim(session: Session) -> None:
    job = make_job(session)
    claim_job(session, job.id, VISIBILITY)
    assert complete_with_score(session, job.id, risk=82) is True
    session.commit()

    stored = get_job(session, job.id)
    assert stored.status == JobStatus.COMPLETED.value
    assert stored.risk_score == 82
    assert stored.sentiment == "negative"
    assert stored.estimated_cost_usd == Decimal("0.00025600")
    assert stored.total_tokens == 460
    assert stored.locked_until is None
    assert stored.completed_at is not None


def test_complete_is_ignored_when_the_claim_was_lost(session: Session) -> None:
    """A slow worker whose claim expired must not overwrite the takeover worker's result."""
    job = make_job(session)
    claim_job(session, job.id, VISIBILITY)
    expire_claim(session, job.id)
    claim_job(session, job.id, VISIBILITY)  # takeover worker
    complete_with_score(session, job.id, risk=10)
    session.commit()

    # The original worker now finishes and tries to write its own result.
    assert complete_with_score(session, job.id, risk=99) is False
    assert get_job(session, job.id).risk_score == 10


def test_release_returns_the_job_to_pending_keeping_attempts(session: Session) -> None:
    job = make_job(session)
    claim_job(session, job.id, VISIBILITY)
    assert release_job_for_retry(session, job.id, ErrorType.TIMEOUT, "timed out") is True
    session.commit()

    stored = get_job(session, job.id)
    assert stored.status == JobStatus.PENDING.value
    assert stored.attempt_count == 1
    assert stored.last_error_type == ErrorType.TIMEOUT.value
    assert stored.locked_until is None


def test_released_job_can_be_claimed_again_immediately(session: Session) -> None:
    job = make_job(session)
    claim_job(session, job.id, VISIBILITY)
    release_job_for_retry(session, job.id, ErrorType.SERVER_ERROR, "503")
    session.commit()

    again = claim_job(session, job.id, VISIBILITY)
    session.commit()
    assert again.outcome is ClaimOutcome.CLAIMED
    assert again.job.attempt_count == 2


def test_fail_is_terminal(session: Session) -> None:
    job = make_job(session)
    claim_job(session, job.id, VISIBILITY)
    assert fail_job(session, job.id, ErrorType.MAX_ATTEMPTS_EXHAUSTED, "gave up") is True
    session.commit()

    stored = get_job(session, job.id)
    assert stored.status == JobStatus.FAILED.value
    assert stored.completed_at is not None
    assert claim_job(session, job.id, VISIBILITY).outcome is ClaimOutcome.ALREADY_FINISHED


def test_long_error_text_is_truncated(session: Session) -> None:
    job = make_job(session)
    claim_job(session, job.id, VISIBILITY)
    fail_job(session, job.id, ErrorType.INTERNAL_ERROR, "x" * 5000)
    session.commit()
    assert len(get_job(session, job.id).last_error) <= 1000


def test_enqueue_failure_marks_the_job_failed(session: Session) -> None:
    """The dual-write gap: a row nothing would ever process is marked, not left pending."""
    job = make_job(session)
    mark_enqueue_failed(session, job.id, "queue unavailable")
    session.commit()

    stored = get_job(session, job.id)
    assert stored.status == JobStatus.FAILED.value
    assert stored.last_error_type == ErrorType.ENQUEUE_FAILED.value


def test_list_filters_and_orders_newest_first(session: Session) -> None:
    first = make_job(session)
    second = make_job(session, source=JobSource.S3, source_ref="s3://bucket/a.json")
    claim_job(session, second.id, VISIBILITY)
    complete_with_score(session, second.id, risk=90)
    session.commit()

    rows, total = list_conversations(session)
    assert total == 2
    assert rows[0].id == second.id  # newest first

    rows, total = list_conversations(session, source=JobSource.S3.value)
    assert [row.id for row in rows] == [second.id]

    rows, total = list_conversations(session, status=JobStatus.PENDING.value)
    assert [row.id for row in rows] == [first.id]

    rows, _ = list_conversations(session, min_risk=95)
    assert rows == []
    rows, _ = list_conversations(session, min_risk=90)
    assert [row.id for row in rows] == [second.id]

    rows, total = list_conversations(session, limit=1, offset=1)
    assert total == 2 and len(rows) == 1


def test_counts_and_stats_aggregate_server_side(session: Session) -> None:
    pending = make_job(session)
    scored = make_job(session)
    claim_job(session, scored.id, VISIBILITY)
    complete_with_score(session, scored.id, risk=60)
    session.commit()

    assert count_by_status(session) == {"pending": 1, "completed": 1}

    summary = stats(session)
    assert summary["total_jobs"] == 2
    assert summary["by_source"] == {"api": 2}
    assert summary["by_sentiment"] == {"negative": 1}
    assert summary["average_risk_score"] == pytest.approx(60.0)
    assert summary["max_risk_score"] == 60
    assert summary["total_tokens"] == 460
    assert summary["estimated_cost_usd"] == Decimal("0.00025600")
    assert summary["average_llm_latency_ms"] == pytest.approx(1200.0)
    assert pending.id is not None


def test_stats_on_an_empty_database_are_zero_not_null(session: Session) -> None:
    summary = stats(session)
    assert summary["total_jobs"] == 0
    assert summary["total_tokens"] == 0
    assert summary["estimated_cost_usd"] == Decimal("0")
    assert summary["average_risk_score"] is None


def test_backlog_age_tracks_the_oldest_unfinished_job(session: Session) -> None:
    assert oldest_pending_age_seconds(session) == 0.0

    job = make_job(session)
    session.execute(
        text("UPDATE conversations SET created_at = now() - interval '120 seconds' WHERE id = :id"),
        {"id": job.id},
    )
    session.commit()
    assert oldest_pending_age_seconds(session) >= 119

    claim_job(session, job.id, VISIBILITY)
    complete_with_score(session, job.id)
    session.commit()
    assert oldest_pending_age_seconds(session) == 0.0


def test_database_rejects_an_out_of_range_risk_score(session: Session) -> None:
    """Defence in depth: even if application validation were bypassed, the row cannot exist."""
    job = make_job(session)
    with pytest.raises(IntegrityError):
        session.execute(
            text("UPDATE conversations SET risk_score = 250 WHERE id = :id"), {"id": job.id}
        )
        session.commit()
    session.rollback()


def test_claim_expiry_uses_database_time_not_worker_time(session: Session) -> None:
    """Worker clocks drift; the claim window must be decided by PostgreSQL."""
    job = make_job(session)
    claim_job(session, job.id, 30)
    session.commit()

    stored = get_job(session, job.id)
    db_now = session.execute(text("SELECT now()")).scalar_one()
    delta = stored.locked_until - db_now
    assert timedelta(seconds=25) < delta <= timedelta(seconds=30)
