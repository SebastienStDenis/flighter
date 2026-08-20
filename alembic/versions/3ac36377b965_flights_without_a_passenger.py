"""Flights without a passenger.

A flight is now just a flight, so `passengers` and `bookings.passenger_id` go and the
dedupe index loses its first column. Dropping it from the key makes rows that were
distinct - the same flight tracked for two people - collide, and the unique index would
refuse to build over them, so the upgrade archives all but the oldest row of each
colliding group first. The partial index skips archived rows, and archiving is what
removal has always meant here, so nothing is deleted.

The downgrade is lossy, and cannot be otherwise: who a booking was for is not recorded
anywhere after the upgrade. It recreates the table with a single placeholder person and
points every booking at them.

Revision ID: 3ac36377b965
Revises: 2beb0050a38e
Create Date: 2026-08-19 21:51:59.682296

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3ac36377b965"
down_revision: str | None = "2beb0050a38e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The whole expression needs its own parentheses: without them Postgres reads the cast
# as closing the index element and rejects the statement.
_DEPARTURE_DATE = sa.literal_column("((scheduled_departure_utc AT TIME ZONE 'UTC')::date)")


def upgrade() -> None:
    op.execute(
        """
        UPDATE bookings SET status = 'archived'
        WHERE status != 'archived'
          AND id NOT IN (
              SELECT min(id) FROM bookings
              WHERE status != 'archived'
              GROUP BY marketing_carrier,
                       marketing_number,
                       (scheduled_departure_utc AT TIME ZONE 'UTC')::date
          )
        """
    )
    op.drop_index("bookings_dedupe", table_name="bookings")
    op.create_index(
        "bookings_dedupe",
        "bookings",
        ["marketing_carrier", "marketing_number", _DEPARTURE_DATE],
        unique=True,
        postgresql_where=sa.text("status != 'archived'"),
    )
    op.drop_column("bookings", "passenger_id")
    op.drop_table("passengers")


def downgrade() -> None:
    op.create_table(
        "passengers",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("is_self", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO passengers (display_name, is_self) VALUES ('Unknown', true)")

    op.add_column("bookings", sa.Column("passenger_id", sa.BigInteger(), nullable=True))
    op.execute("UPDATE bookings SET passenger_id = (SELECT min(id) FROM passengers)")
    op.alter_column("bookings", "passenger_id", nullable=False)
    op.create_foreign_key(None, "bookings", "passengers", ["passenger_id"], ["id"])

    op.drop_index("bookings_dedupe", table_name="bookings")
    op.create_index(
        "bookings_dedupe",
        "bookings",
        ["passenger_id", "marketing_carrier", "marketing_number", _DEPARTURE_DATE],
        unique=True,
        postgresql_where=sa.text("status != 'archived'"),
    )
