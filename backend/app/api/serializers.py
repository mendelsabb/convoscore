"""Database rows to API responses."""

from __future__ import annotations

from app.models import Conversation
from app.schemas import (
    ConversationDetail,
    ConversationSummary,
    JobError,
    JobStatusResponse,
    LlmExecution,
    ScoreResult,
)
from app.scoring.contract import BAND_LABELS, RiskBand, risk_band


def _band(job: Conversation) -> RiskBand | None:
    return risk_band(job.risk_score)


def to_result(job: Conversation) -> ScoreResult | None:
    if job.sentiment is None or job.risk_score is None or job.rationale is None:
        return None
    band = _band(job)
    assert band is not None
    return ScoreResult(
        sentiment=job.sentiment,
        risk_score=job.risk_score,
        risk_band=band.value,
        risk_band_label=BAND_LABELS[band],
        rationale=job.rationale,
    )


def to_error(job: Conversation) -> JobError | None:
    if job.last_error_type is None:
        return None
    return JobError(type=job.last_error_type, message=job.last_error)


def to_job_status(job: Conversation) -> JobStatusResponse:
    return JobStatusResponse(
        job_id=job.id,
        status=job.status,
        source=job.source,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        created_at=job.created_at,
        completed_at=job.completed_at,
        result=to_result(job),
        error=to_error(job),
    )


def to_summary(job: Conversation) -> ConversationSummary:
    band = _band(job)
    return ConversationSummary(
        id=job.id,
        source=job.source,
        source_ref=job.source_ref,
        status=job.status,
        sentiment=job.sentiment,
        risk_score=job.risk_score,
        risk_band=band.value if band else None,
        message_count=job.message_count,
        attempt_count=job.attempt_count,
        created_at=job.created_at,
        completed_at=job.completed_at,
    )


def to_detail(job: Conversation) -> ConversationDetail:
    return ConversationDetail(
        id=job.id,
        source=job.source,
        source_ref=job.source_ref,
        status=job.status,
        conversation=job.conversation,
        message_count=job.message_count,
        result=to_result(job),
        llm=LlmExecution(
            model=job.model,
            prompt_version=job.prompt_version,
            schema_version=job.schema_version,
            prompt_tokens=job.prompt_tokens,
            completion_tokens=job.completion_tokens,
            total_tokens=job.total_tokens,
            llm_latency_ms=job.llm_latency_ms,
            estimated_cost_usd=job.estimated_cost_usd,
            pricing_version=job.pricing_version,
        ),
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        error=to_error(job),
        created_at=job.created_at,
        enqueued_at=job.enqueued_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )
