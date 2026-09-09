"""Track ingested storage objects so polling cannot create duplicate jobs

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingested_objects",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("bucket", sa.String(length=255), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("etag", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="SET NULL"
        ),
        # The duplicate guard. Same key with new content means a new etag, and therefore a new
        # conversation, which is what re-uploading a corrected export should do.
        sa.UniqueConstraint("bucket", "key", "etag", name="uq_ingested_objects_identity"),
        sa.CheckConstraint(
            "status IN ('ingested','invalid','failed')", name="ck_ingested_objects_status"
        ),
    )
    op.create_index("ix_ingested_objects_discovered_at", "ingested_objects", ["discovered_at"])


def downgrade() -> None:
    op.drop_index("ix_ingested_objects_discovered_at", table_name="ingested_objects")
    op.drop_table("ingested_objects")
