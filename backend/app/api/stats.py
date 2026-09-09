"""Aggregates and health detail for the overview page."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import AppSettings, DbSession
from app.db import ping
from app.repository import stats as repository_stats
from app.schemas import (
    DependencyHealth,
    HealthDetailsResponse,
    StatsResponse,
    StatusCounts,
)
from app.scoring.contract import PROMPT_VERSION, SCHEMA_VERSION
from app.scoring.pricing import PRICING_VERSION

router = APIRouter(tags=["overview"])


@router.get("/stats", response_model=StatsResponse, summary="Job and cost aggregates")
def get_stats(session: DbSession) -> StatsResponse:
    data = repository_stats(session)
    counts = data["by_status"]
    return StatsResponse(
        total_jobs=data["total_jobs"],
        by_status=StatusCounts(
            pending=counts.get("pending", 0),
            processing=counts.get("processing", 0),
            completed=counts.get("completed", 0),
            failed=counts.get("failed", 0),
        ),
        by_source=data["by_source"],
        by_sentiment=data["by_sentiment"],
        average_risk_score=data["average_risk_score"],
        max_risk_score=data["max_risk_score"],
        prompt_tokens=data["prompt_tokens"],
        completion_tokens=data["completion_tokens"],
        total_tokens=data["total_tokens"],
        estimated_cost_usd=data["estimated_cost_usd"],
        average_llm_latency_ms=data["average_llm_latency_ms"],
    )


@router.get(
    "/health/details",
    response_model=HealthDetailsResponse,
    summary="Dependency health and effective configuration",
)
def health_details(
    session: DbSession, settings: AppSettings, request: Request
) -> HealthDetailsResponse:
    """Reports dependency health for the UI.

    This is richer than the readiness probe on purpose: a queue outage stops new work being
    accepted but leaves every review endpoint working, so it is worth showing to an operator
    without taking the API out of the load balancer.
    """
    dependencies = [
        DependencyHealth(
            name="postgres",
            healthy=ping(session),
            detail="source of truth for jobs and results",
        ),
        _queue_health(request),
    ]
    return HealthDetailsResponse(
        healthy=all(dependency.healthy for dependency in dependencies),
        dependencies=dependencies,
        config={
            **settings.safe_summary(),
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "pricing_version": PRICING_VERSION,
            # Whether a key is configured is useful to an operator; the value never is.
            "openai_key_configured": settings.openai_key_configured,
        },
    )


def _queue_health(request: Request) -> DependencyHealth:
    """Check the queue without letting a slow or missing queue break the health endpoint."""
    publisher = getattr(request.app.state, "publisher", None)
    queue = getattr(publisher, "queue", None)
    if queue is None:
        return DependencyHealth(
            name="sqs", healthy=True, detail="in-memory publisher (no queue configured)"
        )
    try:
        counts = queue.attributes()
    except Exception as exc:
        return DependencyHealth(name="sqs", healthy=False, detail=str(exc)[:200])
    return DependencyHealth(
        name="sqs",
        healthy=True,
        detail=f"{counts['visible']} waiting, {counts['in_flight']} in flight",
    )
