"""SQLAlchemy models. PostgreSQL is the source of truth for job state and results."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED})


class JobSource(StrEnum):
    API = "api"
    S3 = "s3"


class ErrorType(StrEnum):
    """Bounded set of error classes. Used as a metric label, so it must stay low cardinality."""

    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    CONNECTION_ERROR = "connection_error"
    INVALID_LLM_RESPONSE = "invalid_llm_response"
    CLIENT_ERROR = "client_error"
    ENQUEUE_FAILED = "enqueue_failed"
    MAX_ATTEMPTS_EXHAUSTED = "max_attempts_exhausted"
    INTERNAL_ERROR = "internal_error"


LAST_ERROR_MAX_CHARS = 1000


class Conversation(Base):
    """One conversation is one scoring job: ``id`` is both the conversation id and the job id.

    Modelling them separately would only pay off if a conversation could be re-scored under a new
    prompt version, which is a documented extension rather than a current requirement.
    """

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # --- provenance ---------------------------------------------------------------
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(Text)

    # --- input --------------------------------------------------------------------
    conversation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    conversation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- lifecycle ----------------------------------------------------------------
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=JobStatus.PENDING, index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    # Claim held by a worker; a claim older than now() is expired and may be taken over.
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_type: Mapped[str | None] = mapped_column(String(32))
    last_error: Mapped[str | None] = mapped_column(Text)

    # --- result -------------------------------------------------------------------
    sentiment: Mapped[str | None] = mapped_column(String(16))
    risk_score: Mapped[int | None] = mapped_column(SmallInteger)
    rationale: Mapped[str | None] = mapped_column(Text)

    # --- execution metadata (the raw LLM response is deliberately not stored) ------
    model: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(16))
    schema_version: Mapped[str | None] = mapped_column(String(16))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    llm_latency_ms: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    pricing_version: Mapped[str | None] = mapped_column(String(16))

    # --- timestamps ---------------------------------------------------------------
    # clock_timestamp(), not now(): now() is transaction start time, so a batch of jobs created in
    # one transaction (storage ingestion) would share a timestamp and "newest first" would fall
    # back to an arbitrary tie-break.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','processing','completed','failed')",
            name="ck_conversations_status",
        ),
        CheckConstraint("source IN ('api','s3')", name="ck_conversations_source"),
        CheckConstraint(
            "sentiment IS NULL OR sentiment IN ('positive','neutral','negative')",
            name="ck_conversations_sentiment",
        ),
        CheckConstraint(
            "risk_score IS NULL OR (risk_score >= 0 AND risk_score <= 100)",
            name="ck_conversations_risk_score",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_conversations_attempt_count"),
        Index("ix_conversations_status_created_at", "status", "created_at"),
        Index("ix_conversations_created_at_desc", created_at.desc()),
        Index("ix_conversations_source", "source"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Conversation {self.id} {self.source} {self.status}>"


class IngestStatus(StrEnum):
    INGESTED = "ingested"
    INVALID = "invalid"
    FAILED = "failed"


class IngestedObject(Base):
    """One row per object the ingestor has seen, so polling cannot create duplicate jobs.

    The key is ``(bucket, key, etag)``. Using the etag rather than the key alone means:

    * re-listing the same unchanged object is skipped, however often we poll;
    * re-uploading *different* content to the same key is a new conversation and a new job,
      which is what someone correcting a bad export would expect.

    Objects that fail validation are recorded too. Without that, a malformed file would be
    re-read and re-rejected on every single poll.
    """

    __tablename__ = "ingested_objects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    etag: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )

    __table_args__ = (
        UniqueConstraint("bucket", "key", "etag", name="uq_ingested_objects_identity"),
        CheckConstraint(
            "status IN ('ingested','invalid','failed')", name="ck_ingested_objects_status"
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<IngestedObject s3://{self.bucket}/{self.key} {self.status}>"
