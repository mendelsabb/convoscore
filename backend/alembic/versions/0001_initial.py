"""Initial schema: conversations (one row per conversation and its scoring job)

Revision ID: 0001
Revises:
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        # provenance
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        # input
        sa.Column("conversation", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("conversation_hash", sa.String(length=64), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        # lifecycle
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_type", sa.String(length=32), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        # result
        sa.Column("sentiment", sa.String(length=16), nullable=True),
        sa.Column("risk_score", sa.SmallInteger(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        # execution metadata (the raw LLM response is deliberately not stored)
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("prompt_version", sa.String(length=16), nullable=True),
        sa.Column("schema_version", sa.String(length=16), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("llm_latency_ms", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(precision=12, scale=8), nullable=True),
        sa.Column("pricing_version", sa.String(length=16), nullable=True),
        # timestamps
        # clock_timestamp(), not now(): now() is transaction start time, so a batch of jobs
        # inserted in one transaction would share a timestamp and "newest first" ordering would
        # fall back to an arbitrary tie-break.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','failed')",
            name="ck_conversations_status",
        ),
        sa.CheckConstraint("source IN ('api','s3')", name="ck_conversations_source"),
        sa.CheckConstraint(
            "sentiment IS NULL OR sentiment IN ('positive','neutral','negative')",
            name="ck_conversations_sentiment",
        ),
        sa.CheckConstraint(
            "risk_score IS NULL OR (risk_score >= 0 AND risk_score <= 100)",
            name="ck_conversations_risk_score",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_conversations_attempt_count"),
    )

    op.create_index("ix_conversations_status", "conversations", ["status"])
    op.create_index("ix_conversations_source", "conversations", ["source"])
    # Serves the review list (newest first) and the backlog-age query.
    op.create_index("ix_conversations_status_created_at", "conversations", ["status", "created_at"])
    op.create_index(
        "ix_conversations_created_at_desc",
        "conversations",
        [sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_created_at_desc", table_name="conversations")
    op.drop_index("ix_conversations_status_created_at", table_name="conversations")
    op.drop_index("ix_conversations_source", table_name="conversations")
    op.drop_index("ix_conversations_status", table_name="conversations")
    op.drop_table("conversations")
