"""Keep every confirmation code a booking has, each under its own name.

Revision ID: 9d41e7b8c250
Revises: 51a0c647bd27
Create Date: 2026-08-29 09:41:12.882301

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9d41e7b8c250"
down_revision: str | None = "51a0c647bd27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "bookings",
        sa.Column("confirmations", sa.JSON(), nullable=False, server_default="[]"),
    )
    # The one code a booking could hold becomes the first of the list, unnamed: nothing
    # ever recorded what to call it.
    op.execute(
        """
        UPDATE bookings
        SET confirmations = json_array(json_object('code', confirmation_code, 'name', null))
        WHERE COALESCE(TRIM(confirmation_code), '') != ''
        """
    )
    with op.batch_alter_table("bookings") as batch:
        batch.drop_column("confirmation_code")


def downgrade() -> None:
    op.add_column("bookings", sa.Column("confirmation_code", sa.Text()))
    # Only the first survives; the rest have nowhere to go.
    op.execute(
        """
        UPDATE bookings
        SET confirmation_code = json_extract(confirmations, '$[0].code')
        WHERE json_array_length(confirmations) > 0
        """
    )
    with op.batch_alter_table("bookings") as batch:
        batch.drop_column("confirmations")
