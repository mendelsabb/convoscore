"""Pydantic schemas for the conversation input and the API surface.

The conversation input schema is shared by both ingestion paths: an API request body and an S3
object are validated by exactly the same model, so there is one validator and one pipeline.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_MESSAGES = 50
MAX_TOTAL_CHARS = 8_000
MAX_MESSAGE_CHARS = 4_000


class Role(StrEnum):
    CUSTOMER = "customer"
    AGENT = "agent"


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["customer", "agent"]
    content: Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE_CHARS)]


class ConversationMetadata(BaseModel):
    """Optional, non-sensitive routing information. Never send PII here."""

    model_config = ConfigDict(extra="forbid")

    channel: str | None = Field(default=None, max_length=64)
    tags: list[Annotated[str, Field(max_length=64)]] = Field(default_factory=list, max_length=10)
    external_id: str | None = Field(default=None, max_length=128)


class ConversationInput(BaseModel):
    """A conversation to score, from either ingestion path."""

    model_config = ConfigDict(extra="forbid")

    messages: Annotated[list[Message], Field(min_length=1, max_length=MAX_MESSAGES)]
    metadata: ConversationMetadata = Field(default_factory=ConversationMetadata)

    @model_validator(mode="after")
    def check_total_size(self) -> ConversationInput:
        total = sum(len(message.content) for message in self.messages)
        if total > MAX_TOTAL_CHARS:
            raise ValueError(
                f"conversation is {total} characters, limit is {MAX_TOTAL_CHARS}"
            )
        if not any(message.role == Role.CUSTOMER for message in self.messages):
            raise ValueError("conversation must contain at least one customer message")
        return self

    def normalized(self) -> dict[str, Any]:
        """The canonical JSONB payload stored in the database."""
        return {
            "messages": [{"role": m.role, "content": m.content} for m in self.messages],
            "metadata": self.metadata.model_dump(exclude_none=True),
        }

    def content_hash(self) -> str:
        """Stable hash of the message content, used for de-duplication and safe logging.

        Metadata is excluded on purpose: the same transcript tagged differently is the same
        conversation.
        """
        canonical = json.dumps(
            [{"role": m.role, "content": m.content} for m in self.messages],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------------------


class JobCreatedResponse(BaseModel):
    job_id: uuid.UUID
    status: str


class ScoreResult(BaseModel):
    sentiment: str
    risk_score: int
    risk_band: str
    risk_band_label: str
    rationale: str


class JobError(BaseModel):
    type: str
    message: str | None = None


class JobStatusResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    source: str
    attempt_count: int
    max_attempts: int
    created_at: datetime
    completed_at: datetime | None = None
    result: ScoreResult | None = None
    error: JobError | None = None


class ConversationSummary(BaseModel):
    id: uuid.UUID
    source: str
    source_ref: str | None
    status: str
    sentiment: str | None
    risk_score: int | None
    risk_band: str | None
    message_count: int
    attempt_count: int
    created_at: datetime
    completed_at: datetime | None


class ConversationListResponse(BaseModel):
    items: list[ConversationSummary]
    total: int
    limit: int
    offset: int


class LlmExecution(BaseModel):
    model: str | None
    prompt_version: str | None
    schema_version: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    llm_latency_ms: int | None
    estimated_cost_usd: Decimal | None
    pricing_version: str | None


class ConversationDetail(BaseModel):
    id: uuid.UUID
    source: str
    source_ref: str | None
    status: str
    conversation: dict[str, Any]
    message_count: int
    result: ScoreResult | None
    llm: LlmExecution
    attempt_count: int
    max_attempts: int
    error: JobError | None
    created_at: datetime
    enqueued_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None


class StatusCounts(BaseModel):
    pending: int = 0
    processing: int = 0
    completed: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        return self.pending + self.processing + self.completed + self.failed


class StatsResponse(BaseModel):
    total_jobs: int
    by_status: StatusCounts
    by_source: dict[str, int]
    by_sentiment: dict[str, int]
    average_risk_score: float | None
    max_risk_score: int | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: Decimal
    average_llm_latency_ms: float | None


class DependencyHealth(BaseModel):
    name: str
    healthy: bool
    detail: str | None = None


class HealthDetailsResponse(BaseModel):
    healthy: bool
    dependencies: list[DependencyHealth]
    config: dict[str, Any]


# --------------------------------------------------------------------------------------
# Demo controls (mounted only when DEMO_MODE is true)
# --------------------------------------------------------------------------------------


class DemoArmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["timeout", "http_500", "malformed"]
    # Capped: the control is for demonstrating failure handling, not for disabling scoring.
    count: Annotated[int, Field(ge=0, le=20)] = 1


class DemoStateResponse(BaseModel):
    armed: dict[str, int]
    total_armed: int
    modes: list[str]
