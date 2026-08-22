"""Normalize Endeavor Air operating flight identifiers.

Revision ID: 51a0c647bd27
Revises: c4e8a1f6d2b9
Create Date: 2026-08-22 15:32:44.945598

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "51a0c647bd27"
down_revision: str | None = "c4e8a1f6d2b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE bookings
        SET operating_carrier = '9E',
            operating_number = COALESCE(NULLIF(TRIM(operating_number), ''), marketing_number)
        WHERE UPPER(TRIM(operating_carrier)) = 'ENDEAVOR AIR'
        """
    )


def downgrade() -> None:
    pass
