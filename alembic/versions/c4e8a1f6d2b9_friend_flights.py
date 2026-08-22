"""Keep the friend a tracked flight belongs to.

Revision ID: c4e8a1f6d2b9
Revises: b7c2d9e4f1a6
Create Date: 2026-08-22 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8a1f6d2b9"
down_revision: str | None = "b7c2d9e4f1a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("friend_name", sa.Text()))


def downgrade() -> None:
    with op.batch_alter_table("bookings") as batch:
        batch.drop_column("friend_name")
