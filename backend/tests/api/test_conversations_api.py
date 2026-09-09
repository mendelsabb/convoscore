"""The HTTP contract: submit, poll, browse, inspect."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ErrorType, JobSource, JobStatus
from app.queue import FailingPublisher
from app.repository import claim_job, complete_job, create_job, get_job
from app.schemas import ConversationInput
from tests.conftest import requires_postgres, sample_conversation

pytestmark = [requires_postgres, pytest.mark.integration]


def submit(client: TestClient, payload: dict | None = None) -> uuid.UUID:
    """Submit a conversation and return its job id."""
    response = client.post("/api/conversations", json=payload or sample_conversation())
    assert response.status_code == 202, response.text
    return uuid.UUID(response.json()["job_id"])


def test_submit_returns_202_with_a_job_id(client: TestClient, publisher) -> None:
    response = client.post("/api/conversations", json=sample_conversation())

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == JobStatus.PENDING.value
    job_id = uuid.UUID(body["job_id"])
    assert publisher.published == [job_id]


def test_submitted_job_is_persisted_as_pending(client: TestClient, session: Session) -> None:
    job_id = submit(client)

    job = get_job(session, job_id)
    assert job.status == JobStatus.PENDING.value
    assert job.source == JobSource.API.value
    assert job.enqueued_at is not None
    assert job.conversation["messages"][0]["role"] == "customer"


def test_enqueue_failure_returns_503_and_marks_the_job_failed(
    client: TestClient, session: Session
) -> None:
    """The dual-write gap is surfaced to the caller rather than silently stranding a row."""
    client.app.state.publisher = FailingPublisher("localstack is down")

    response = client.post("/api/conversations", json=sample_conversation())

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == JobStatus.FAILED.value
    job = get_job(session, uuid.UUID(body["job_id"]))
    assert job.status == JobStatus.FAILED.value
    assert job.last_error_type == ErrorType.ENQUEUE_FAILED.value


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        pytest.param({"messages": []}, "no messages", id="empty"),
        pytest.param(
            {"messages": [{"role": "agent", "content": "hi"}]}, "no customer", id="agent-only"
        ),
        pytest.param(
            {"messages": [{"role": "bot", "content": "hi"}]}, "bad role", id="unknown-role"
        ),
        pytest.param({"messages": [{"role": "customer"}]}, "missing content", id="no-content"),
        pytest.param(
            {"messages": [{"role": "customer", "content": "x" * 9000}]}, "too big", id="too-large"
        ),
    ],
)
def test_invalid_submissions_are_rejected(client: TestClient, payload: dict, reason: str) -> None:
    assert client.post("/api/conversations", json=payload).status_code == 422, reason


def test_poll_a_pending_job(client: TestClient) -> None:
    job_id = submit(client)

    body = client.get(f"/api/jobs/{job_id}").json()
    assert body["status"] == JobStatus.PENDING.value
    assert body["attempt_count"] == 0
    assert body["result"] is None
    assert body["error"] is None


def test_poll_a_completed_job_returns_the_score(client: TestClient, session: Session) -> None:
    job_id = submit(client)
    _score(session, job_id, risk=88)

    body = client.get(f"/api/jobs/{job_id}").json()
    assert body["status"] == JobStatus.COMPLETED.value
    assert body["result"]["risk_score"] == 88
    assert body["result"]["risk_band"] == "urgent_escalation"
    assert body["result"]["risk_band_label"] == "Urgent / escalation"
    assert body["result"]["sentiment"] == "negative"


def test_unknown_job_is_404(client: TestClient) -> None:
    assert client.get(f"/api/jobs/{uuid.uuid4()}").status_code == 404


def test_malformed_job_id_is_422(client: TestClient) -> None:
    assert client.get("/api/jobs/not-a-uuid").status_code == 422


def test_browse_lists_newest_first_with_a_total(client: TestClient, session: Session) -> None:
    first = submit(client)
    second = submit(client)

    body = client.get("/api/conversations").json()
    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == [str(second), str(first)]


def test_browse_filters(client: TestClient, session: Session) -> None:
    pending = submit(client)
    scored = submit(client)
    _score(session, scored, risk=20)

    completed = client.get("/api/conversations", params={"status": "completed"}).json()
    assert [item["id"] for item in completed["items"]] == [str(scored)]

    by_source = client.get("/api/conversations", params={"source": "api"}).json()
    assert by_source["total"] == 2

    high_risk = client.get("/api/conversations", params={"min_risk": 50}).json()
    assert high_risk["items"] == []

    by_sentiment = client.get("/api/conversations", params={"sentiment": "negative"}).json()
    assert [item["id"] for item in by_sentiment["items"]] == [str(scored)]
    assert pending is not None


def test_browse_rejects_unknown_filter_values(client: TestClient) -> None:
    assert client.get("/api/conversations", params={"status": "banana"}).status_code == 422
    assert client.get("/api/conversations", params={"source": "ftp"}).status_code == 422
    assert client.get("/api/conversations", params={"min_risk": 500}).status_code == 422


def test_detail_exposes_transcript_and_execution_metadata(
    client: TestClient, session: Session
) -> None:
    job_id = submit(client)
    _score(session, job_id, risk=45)

    body = client.get(f"/api/conversations/{job_id}").json()
    assert body["status"] == JobStatus.COMPLETED.value
    assert len(body["conversation"]["messages"]) == 3
    assert body["result"]["risk_band"] == "moderate_concern"
    assert body["llm"]["model"] == "gpt-4.1-mini"
    assert body["llm"]["prompt_version"] == "v1"
    assert body["llm"]["total_tokens"] == 460
    assert body["llm"]["estimated_cost_usd"] == "0.00025600"
    assert body["attempt_count"] == 1


def test_detail_of_an_unscored_job_has_no_result(client: TestClient) -> None:
    job_id = submit(client)
    body = client.get(f"/api/conversations/{job_id}").json()
    assert body["result"] is None
    assert body["llm"]["model"] is None


def test_stats_summarise_jobs_tokens_and_cost(client: TestClient, session: Session) -> None:
    client.post("/api/conversations", json=sample_conversation())
    scored = submit(client)
    _score(session, scored, risk=70)

    body = client.get("/api/stats").json()
    assert body["total_jobs"] == 2
    assert body["by_status"]["pending"] == 1
    assert body["by_status"]["completed"] == 1
    assert body["by_source"] == {"api": 2}
    assert body["by_sentiment"] == {"negative": 1}
    assert body["average_risk_score"] == 70.0
    assert body["total_tokens"] == 460
    assert body["estimated_cost_usd"] == "0.00025600"


def test_stats_are_safe_on_an_empty_database(client: TestClient) -> None:
    body = client.get("/api/stats").json()
    assert body["total_jobs"] == 0
    assert body["average_risk_score"] is None
    assert body["estimated_cost_usd"] == "0"


def test_health_details_reports_dependencies_and_versions(client: TestClient) -> None:
    body = client.get("/api/health/details").json()
    assert body["healthy"] is True
    assert [dependency["name"] for dependency in body["dependencies"]] == ["postgres", "sqs"]
    assert body["config"]["prompt_version"] == "v1"
    assert body["config"]["schema_version"] == "1"
    assert body["config"]["pricing_version"] == "2026-09"


def test_health_details_never_exposes_the_api_key(client: TestClient) -> None:
    """It reports whether a key is configured, never the key itself."""
    body = client.get("/api/health/details").json()
    assert body["config"]["openai_key_configured"] in (True, False)
    assert "sk-" not in json.dumps(body)


def test_openapi_documents_the_public_api(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    for path in (
        "/api/conversations",
        "/api/conversations/{conversation_id}",
        "/api/jobs/{job_id}",
        "/api/stats",
        "/healthz",
        "/readyz",
    ):
        assert path in paths


def _score(session: Session, job_id: uuid.UUID, risk: int) -> None:
    claim_job(session, job_id, 90)
    complete_job(
        session,
        job_id,
        sentiment="negative",
        risk_score=risk,
        rationale="Customer chased three times without a resolution.",
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
    session.commit()


def test_create_job_helper_is_shared_by_both_sources(session: Session) -> None:
    """Storage ingestion (milestone 4) reuses exactly this call with source=s3."""
    job = create_job(
        session,
        ConversationInput.model_validate(sample_conversation()),
        source=JobSource.S3,
        max_attempts=3,
        source_ref="s3://convoscore/incoming/a.json",
    )
    session.commit()
    assert job.source == JobSource.S3.value
    assert job.source_ref == "s3://convoscore/incoming/a.json"
