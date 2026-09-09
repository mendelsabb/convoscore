"""Conversation submission and human-review endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status

from app import metrics
from app.api.deps import AppSettings, DbSession, Publisher
from app.api.serializers import to_detail, to_job_status, to_summary
from app.logging import get_logger
from app.models import ErrorType, JobSource, JobStatus
from app.queue import QueuePublishError
from app.repository import (
    create_job,
    get_job,
    list_conversations,
    mark_enqueue_failed,
    mark_enqueued,
)
from app.schemas import (
    ConversationDetail,
    ConversationInput,
    ConversationListResponse,
    JobCreatedResponse,
    JobStatusResponse,
)

log = get_logger(__name__)

router = APIRouter(tags=["conversations"])


@router.post(
    "/conversations",
    response_model=JobCreatedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a conversation for scoring",
    responses={503: {"description": "Job recorded but could not be queued"}},
)
def submit_conversation(
    payload: ConversationInput,
    session: DbSession,
    publisher: Publisher,
    settings: AppSettings,
    response: Response,
) -> JobCreatedResponse:
    """Validate, persist a pending job, enqueue its id, and return immediately.

    The scoring itself happens asynchronously in a worker: this handler never calls the LLM, so
    its latency and availability do not depend on OpenAI.
    """
    job = create_job(
        session,
        conversation=payload,
        source=JobSource.API,
        max_attempts=settings.max_attempts,
    )
    job_id = job.id

    try:
        publisher.publish(job_id)
    except QueuePublishError as exc:
        # The database write succeeded but the queue send did not. Rather than leave a row that
        # nothing will ever process, mark it failed and tell the client, who can resubmit.
        mark_enqueue_failed(session, job_id, str(exc))
        session.commit()
        metrics.enqueue_failures_total.labels(source=JobSource.API.value).inc()
        metrics.jobs_failed_total.labels(
            source=JobSource.API.value, error_type=ErrorType.ENQUEUE_FAILED.value
        ).inc()
        log.error(
            "job_enqueue_failed",
            extra={"job_id": str(job_id), "source": JobSource.API.value, "error": str(exc)},
        )
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return JobCreatedResponse(job_id=job_id, status=JobStatus.FAILED.value)

    mark_enqueued(session, job_id)
    metrics.jobs_created_total.labels(source=JobSource.API.value).inc()
    log.info(
        "job_created",
        extra={
            "job_id": str(job_id),
            "source": JobSource.API.value,
            "message_count": len(payload.messages),
            "conversation_hash": payload.content_hash()[:12],
        },
    )
    return JobCreatedResponse(job_id=job_id, status=JobStatus.PENDING.value)


@router.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    summary="Poll a scoring job",
)
def get_job_status(job_id: uuid.UUID, session: DbSession) -> JobStatusResponse:
    job = get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return to_job_status(job)


@router.get(
    "/conversations",
    response_model=ConversationListResponse,
    summary="Browse scored conversations",
)
def browse_conversations(
    session: DbSession,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    source: Annotated[str | None, Query()] = None,
    sentiment: Annotated[str | None, Query()] = None,
    min_risk: Annotated[int | None, Query(ge=0, le=100)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ConversationListResponse:
    _validate_enum("status", status_filter, {s.value for s in JobStatus})
    _validate_enum("source", source, {s.value for s in JobSource})
    _validate_enum("sentiment", sentiment, {"positive", "neutral", "negative"})

    rows, total = list_conversations(
        session,
        status=status_filter,
        source=source,
        sentiment=sentiment,
        min_risk=min_risk,
        limit=limit,
        offset=offset,
    )
    return ConversationListResponse(
        items=[to_summary(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetail,
    summary="Inspect one scored conversation",
)
def get_conversation(conversation_id: uuid.UUID, session: DbSession) -> ConversationDetail:
    job = get_job(session, conversation_id)
    if job is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return to_detail(job)


def _validate_enum(name: str, value: str | None, allowed: set[str]) -> None:
    if value is not None and value not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"{name} must be one of {sorted(allowed)}",
        )
