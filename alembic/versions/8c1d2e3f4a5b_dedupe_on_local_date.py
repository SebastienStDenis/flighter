"""Dedupe on the departure's local date, and drop what nothing reads.

Revision ID: 8c1d2e3f4a5b
Revises: 62af024c7198
Create Date: 2026-08-20 14:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import op

revision: str = "8c1d2e3f4a5b"
down_revision: str | None = "62af024c7198"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_STATUSES = "status IN ('pending_review', 'active', 'completed', 'archived')"
_WITH_CANCELLED = "status IN ('pending_review', 'active', 'completed', 'cancelled', 'archived')"
_POLL_WHERE = sa.text("status = 'active' AND next_poll_at IS NOT NULL")
_DEDUPE_WHERE = sa.text("status != 'archived'")


def upgrade() -> None:
    # The indexes are rebuilt by hand on either side of the table copy: a partial or an
    # expression index does not survive SQLite reflection intact.
    op.drop_index("bookings_dedupe", table_name="bookings", sqlite_where=_DEDUPE_WHERE)
    op.drop_index("bookings_poll", table_name="bookings", sqlite_where=_POLL_WHERE)

    op.add_column("bookings", sa.Column("departure_local_date", sa.Date(), nullable=True))
    _backfill_local_dates()

    with op.batch_alter_table("bookings") as batch:
        batch.alter_column("departure_local_date", existing_type=sa.Date(), nullable=False)
        batch.drop_column("aeroapi_ident")
        batch.drop_constraint("bookings_status_check", type_="check")
        batch.create_check_constraint("bookings_status_check", _ACTIVE_STATUSES)

    op.create_index(
        "bookings_dedupe",
        "bookings",
        ["marketing_carrier", "marketing_number", "departure_local_date"],
        unique=True,
        sqlite_where=_DEDUPE_WHERE,
    )
    op.create_index("bookings_poll", "bookings", ["next_poll_at"], sqlite_where=_POLL_WHERE)

    with op.batch_alter_table("api_usage") as batch:
        batch.drop_column("result_sets")


def downgrade() -> None:
    op.drop_index("bookings_dedupe", table_name="bookings", sqlite_where=_DEDUPE_WHERE)
    op.drop_index("bookings_poll", table_name="bookings", sqlite_where=_POLL_WHERE)

    with op.batch_alter_table("bookings") as batch:
        batch.add_column(sa.Column("aeroapi_ident", sa.Text(), nullable=True))
        batch.drop_column("departure_local_date")
        batch.drop_constraint("bookings_status_check", type_="check")
        batch.create_check_constraint("bookings_status_check", _WITH_CANCELLED)

    op.create_index(
        "bookings_dedupe",
        "bookings",
        ["marketing_carrier", "marketing_number", sa.text("date(scheduled_departure_utc)")],
        unique=True,
        sqlite_where=_DEDUPE_WHERE,
    )
    op.create_index("bookings_poll", "bookings", ["next_poll_at"], sqlite_where=_POLL_WHERE)

    with op.batch_alter_table("api_usage") as batch:
        batch.add_column(sa.Column("result_sets", sa.Integer(), nullable=False, server_default="1"))


def _backfill_local_dates() -> None:
    """The date at the origin airport for every existing booking.

    SQLite cannot convert zones, so the rows are read out and written back one by one;
    there are a few hundred a year. A booking whose airport is not in the table falls
    back to the UTC day, which is what the old index keyed on.
    """
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT b.id, b.scheduled_departure_utc, a.tz FROM bookings b "
            "LEFT JOIN airports a ON a.iata = b.origin_iata"
        )
    ).all()
    for booking_id, departure, tz in rows:
        instant = datetime.fromisoformat(str(departure)).replace(tzinfo=UTC)
        local = instant.astimezone(ZoneInfo(tz)) if tz else instant
        bind.execute(
            sa.text("UPDATE bookings SET departure_local_date = :day WHERE id = :id"),
            {"day": local.date().isoformat(), "id": booking_id},
        )
