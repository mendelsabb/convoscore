"""All database access for jobs.

The reliability of the pipeline lives here. Every state transition is a single conditional
``UPDATE`` guarded by the current status and the claim expiry, so two workers handed the same SQS
message cannot both score it, and a duplicate delivery of a finished job does no work at all.

Time comes from the database (``now()``), never from the worker's clock: several worker pods with
skewed clocks must agree on whether a claim has expired.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum, auto
from typing import Any

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.orm import Session

from app.models import LAST_ERROR_MAX_CHARS, Conversation, ErrorType, JobSource, JobStatus
from app.schemas import ConversationInput

# --------------------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------------------


def create_job(
    session: Session,
    conversation: ConversationInput,
    source: JobSource,
    max_attempts: int,
    source_ref: str | None = None,
) -> Conversation:
    """Insert a pending job. The caller enqueues the id afterwards."""
    job = Conversation(
        id=uuid.uuid4(),
        source=source.value,
        source_ref=source_ref,
        conversation=conversation.normalized(),
        conversation_hash=conversation.content_hash(),
        message_count=len(conversation.messages),
        status=JobStatus.PENDING.value,
        attempt_count=0,
        max_attempts=max_attempts,
    )
    session.add(job)
    session.flush()
    return job


def mark_enqueued(session: Session, job_id: uuid.UUID) -> None:
    session.execute(
        update(Conversation)
        .where(Conversation.id == job_id)
        .values(enqueued_at=func.now(), updated_at=func.now())
        .execution_options(synchronize_session=False)
    )


def mark_enqueue_failed(session: Session, job_id: uuid.UUID, error: str) -> None:
    """The dual-write gap made visible: the row exists but nothing will ever process it."""
    session.execute(
        update(Conversation)
        .where(Conversation.id == job_id)
        .values(
            status=JobStatus.FAILED.value,
            last_error_type=ErrorType.ENQUEUE_FAILED.value,
            last_error=_truncate(error),
            completed_at=func.now(),
            updated_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )


# --------------------------------------------------------------------------------------
# Claiming and finishing
# --------------------------------------------------------------------------------------


class ClaimOutcome(StrEnum):
    CLAIMED = auto()
    ALREADY_FINISHED = auto()
    HELD_BY_ANOTHER_WORKER = auto()
    NOT_FOUND = auto()


@dataclass(frozen=True)
class ClaimResult:
    outcome: ClaimOutcome
    job: Conversation | None = None

    @property
    def claimed(self) -> bool:
        return self.outcome is ClaimOutcome.CLAIMED


def claim_job(session: Session, job_id: uuid.UUID, visibility_timeout_seconds: int) -> ClaimResult:
    """Atomically take ownership of a job for one attempt.

    Succeeds only when the job is still pending or processing *and* no live claim exists. The
    attempt counter is incremented as part of the same statement, so an attempt can never be lost
    or double counted.
    """
    claim_expiry = func.now() + func.make_interval(0, 0, 0, 0, 0, 0, visibility_timeout_seconds)
    statement = (
        update(Conversation)
        .where(
            Conversation.id == job_id,
            Conversation.status.in_([JobStatus.PENDING.value, JobStatus.PROCESSING.value]),
            or_(Conversation.locked_until.is_(None), Conversation.locked_until < func.now()),
        )
        .values(
            status=JobStatus.PROCESSING.value,
            attempt_count=Conversation.attempt_count + 1,
            started_at=func.coalesce(Conversation.started_at, func.now()),
            locked_until=claim_expiry,
            updated_at=func.now(),
        )
        .returning(Conversation.id)
        .execution_options(synchronize_session=False)
    )

    claimed_id = session.execute(statement).scalar_one_or_none()
    if claimed_id is not None:
        job = session.get(Conversation, job_id, populate_existing=True)
        return ClaimResult(ClaimOutcome.CLAIMED, job)

    # Nothing was updated: work out why, because the worker reacts differently to each case.
    job = session.get(Conversation, job_id, populate_existing=True)
    if job is None:
        return ClaimResult(ClaimOutcome.NOT_FOUND)
    if job.status in (JobStatus.COMPLETED.value, JobStatus.FAILED.value):
        return ClaimResult(ClaimOutcome.ALREADY_FINISHED, job)
    return ClaimResult(ClaimOutcome.HELD_BY_ANOTHER_WORKER, job)


def complete_job(
    session: Session,
    job_id: uuid.UUID,
    *,
    sentiment: str,
    risk_score: int,
    rationale: str,
    model: str,
    prompt_version: str,
    schema_version: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
    llm_latency_ms: int | None,
    estimated_cost_usd: Decimal | None,
    pricing_version: str | None,
) -> bool:
    """Persist a successful score. Returns False if this worker no longer owns the job."""
    result = session.execute(
        update(Conversation)
        .where(Conversation.id == job_id, Conversation.status == JobStatus.PROCESSING.value)
        .values(
            status=JobStatus.COMPLETED.value,
            sentiment=sentiment,
            risk_score=risk_score,
            rationale=rationale,
            model=model,
            prompt_version=prompt_version,
            schema_version=schema_version,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            llm_latency_ms=llm_latency_ms,
            estimated_cost_usd=estimated_cost_usd,
            pricing_version=pricing_version,
            last_error_type=None,
            last_error=None,
            locked_until=None,
            completed_at=func.now(),
            updated_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def release_job_for_retry(
    session: Session, job_id: uuid.UUID, error_type: ErrorType, error: str
) -> bool:
    """Return a job to pending after a transient failure, keeping the attempt count.

    The SQS message is not deleted; its visibility timeout is extended instead, so the retry
    survives this worker dying.
    """
    result = session.execute(
        update(Conversation)
        .where(Conversation.id == job_id, Conversation.status == JobStatus.PROCESSING.value)
        .values(
            status=JobStatus.PENDING.value,
            last_error_type=error_type.value,
            last_error=_truncate(error),
            locked_until=None,
            updated_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def fail_job(session: Session, job_id: uuid.UUID, error_type: ErrorType, error: str) -> bool:
    """Terminal failure: a permanent error, or the last attempt has been used."""
    result = session.execute(
        update(Conversation)
        .where(
            Conversation.id == job_id,
            Conversation.status.in_([JobStatus.PENDING.value, JobStatus.PROCESSING.value]),
        )
        .values(
            status=JobStatus.FAILED.value,
            last_error_type=error_type.value,
            last_error=_truncate(error),
            locked_until=None,
            completed_at=func.now(),
            updated_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


# --------------------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------------------


def get_job(session: Session, job_id: uuid.UUID) -> Conversation | None:
    """Read a job as it currently exists in the database.

    ``populate_existing`` is deliberate: every state transition above is a bulk UPDATE, which does
    not refresh objects already loaded in this session. Without it a worker that claimed, scored
    and then re-read a job inside one session would see its own pre-update copy.
    """
    return session.get(Conversation, job_id, populate_existing=True)


def list_conversations(
    session: Session,
    *,
    status: str | None = None,
    source: str | None = None,
    sentiment: str | None = None,
    min_risk: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Conversation], int]:
    """Newest first, with the total matching count for pagination."""

    def apply_filters[T](statement: Select[T]) -> Select[T]:
        if status:
            statement = statement.where(Conversation.status == status)
        if source:
            statement = statement.where(Conversation.source == source)
        if sentiment:
            statement = statement.where(Conversation.sentiment == sentiment)
        if min_risk is not None:
            statement = statement.where(Conversation.risk_score >= min_risk)
        return statement

    total = session.execute(
        apply_filters(select(func.count()).select_from(Conversation))
    ).scalar_one()

    rows = session.execute(
        apply_filters(select(Conversation))
        .order_by(Conversation.created_at.desc(), Conversation.id.desc())
        .limit(limit)
        .offset(offset)
    ).scalars()

    return list(rows), int(total)


def count_by_status(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(Conversation.status, func.count()).group_by(Conversation.status)
    ).all()
    return {status: int(count) for status, count in rows}


def oldest_pending_age_seconds(session: Session) -> float:
    """Age of the oldest unfinished job. The backlog signal for dashboards and alerts."""
    oldest = session.execute(
        select(func.min(Conversation.created_at)).where(
            Conversation.status.in_([JobStatus.PENDING.value, JobStatus.PROCESSING.value])
        )
    ).scalar_one_or_none()
    if oldest is None:
        return 0.0
    now: datetime = session.execute(select(func.now())).scalar_one()
    return max(0.0, (now - oldest).total_seconds())


def stats(session: Session) -> dict[str, Any]:
    """Aggregates for the overview page. One query per grouping, all server-side."""
    by_status = count_by_status(session)

    by_source = {
        source: int(count)
        for source, count in session.execute(
            select(Conversation.source, func.count()).group_by(Conversation.source)
        ).all()
    }

    by_sentiment = {
        sentiment: int(count)
        for sentiment, count in session.execute(
            select(Conversation.sentiment, func.count())
            .where(Conversation.sentiment.is_not(None))
            .group_by(Conversation.sentiment)
        ).all()
    }

    totals = session.execute(
        select(
            func.count(),
            func.avg(Conversation.risk_score),
            func.max(Conversation.risk_score),
            func.coalesce(func.sum(Conversation.prompt_tokens), 0),
            func.coalesce(func.sum(Conversation.completion_tokens), 0),
            func.coalesce(func.sum(Conversation.total_tokens), 0),
            func.coalesce(func.sum(Conversation.estimated_cost_usd), Decimal("0")),
            func.avg(Conversation.llm_latency_ms),
        )
    ).one()

    return {
        "total_jobs": int(totals[0]),
        "by_status": by_status,
        "by_source": by_source,
        "by_sentiment": by_sentiment,
        "average_risk_score": float(totals[1]) if totals[1] is not None else None,
        "max_risk_score": int(totals[2]) if totals[2] is not None else None,
        "prompt_tokens": int(totals[3]),
        "completion_tokens": int(totals[4]),
        "total_tokens": int(totals[5]),
        "estimated_cost_usd": Decimal(totals[6]),
        "average_llm_latency_ms": float(totals[7]) if totals[7] is not None else None,
    }


def _truncate(text: str) -> str:
    """Keep error text bounded; it is stored per job and shown in the UI."""
    if len(text) <= LAST_ERROR_MAX_CHARS:
        return text
    return text[: LAST_ERROR_MAX_CHARS - 1] + "…"
