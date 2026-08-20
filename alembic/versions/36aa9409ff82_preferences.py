"""Preferences, so the settings page has somewhere to write.

Revision ID: 36aa9409ff82
Revises: bbba13a0cff1
Create Date: 2026-08-19 20:47:32.149362

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "36aa9409ff82"
down_revision: str | None = "bbba13a0cff1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "preferences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "values", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("id = 1", name="preferences_singleton"),
    )


def downgrade() -> None:
    op.drop_table("preferences")
