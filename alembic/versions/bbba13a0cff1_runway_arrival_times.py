"""Runway arrival times, so a countdown in the air can target wheels down.

Revision ID: bbba13a0cff1
Revises: 74ca7df96720
Create Date: 2026-08-19 20:20:29.433651

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bbba13a0cff1"
down_revision: str | None = "74ca7df96720"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "flight_snapshots", sa.Column("scheduled_on", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "flight_snapshots", sa.Column("estimated_on", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("flight_snapshots", "estimated_on")
    op.drop_column("flight_snapshots", "scheduled_on")
