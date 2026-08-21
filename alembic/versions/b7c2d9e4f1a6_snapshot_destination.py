"""Keep the airport each observed leg is bound for, so a diversion can say where to.

Revision ID: b7c2d9e4f1a6
Revises: 3f7a9b2c1d4e
Create Date: 2026-08-21 17:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c2d9e4f1a6"
down_revision: str | None = "3f7a9b2c1d4e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable and unfilled for rows already written: a past snapshot's destination is
    # the booking's, and nothing reads this column except for a flight marked diverted.
    op.add_column("flight_snapshots", sa.Column("destination_iata", sa.String(3)))


def downgrade() -> None:
    with op.batch_alter_table("flight_snapshots") as batch:
        batch.drop_column("destination_iata")
