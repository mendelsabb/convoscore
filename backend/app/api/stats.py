"""Aggregates and health detail for the overview page."""

from __future__ import annotations

from fastapi import APIRouter

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
def health_details(session: DbSession, settings: AppSettings) -> HealthDetailsResponse:
    """Reports dependency health for the UI.

    This is richer than the readiness probe on purpose: a degraded queue or object store is worth
    showing to an operator, but must not take the API out of the load balancer.
    """
    dependencies = [
        DependencyHealth(
            name="postgres",
            healthy=ping(session),
            detail="source of truth for jobs and results",
        )
    ]
    return HealthDetailsResponse(
        healthy=all(dependency.healthy for dependency in dependencies),
        dependencies=dependencies,
        config={
            **settings.safe_summary(),
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
        },
    )
