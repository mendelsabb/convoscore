"""Demo-only failure injection tokens

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # One row per failure mode, holding how many upcoming scoring calls should fail.
    #
    # This lives in the database rather than in a process's memory because there are two worker
    # replicas: an in-memory flag would fail whichever pod happened to hold it, and the demo would
    # be a coin toss. A row is shared, and decrementing it is atomic.
    op.create_table(
        "demo_failure_tokens",
        sa.Column("mode", sa.String(length=16), primary_key=True),
        sa.Column("remaining", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("armed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("remaining >= 0", name="ck_demo_failure_tokens_remaining"),
        sa.CheckConstraint(
            "mode IN ('timeout','http_500','malformed')", name="ck_demo_failure_tokens_mode"
        ),
    )


def downgrade() -> None:
    op.drop_table("demo_failure_tokens")
