"""Retire the review status: every imported flight goes straight onto the board.

Revision ID: 3f7a9b2c1d4e
Revises: 8c1d2e3f4a5b
Create Date: 2026-08-21 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3f7a9b2c1d4e"
down_revision: str | None = "8c1d2e3f4a5b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = "status IN ('active', 'completed', 'archived')"
_WITH_REVIEW = "status IN ('pending_review', 'active', 'completed', 'archived')"
_POLL_WHERE = sa.text("status = 'active' AND next_poll_at IS NOT NULL")
_DEDUPE_WHERE = sa.text("status != 'archived'")


def upgrade() -> None:
    # A booking that was waiting on somebody's say-so is now simply tracked. Due straight
    # away: the poller works out the real cadence from the first observation, and a row
    # that was never polled has no next time of its own to keep.
    op.execute(
        "UPDATE bookings SET status = 'active', next_poll_at = CURRENT_TIMESTAMP "
        "WHERE status = 'pending_review'"
    )
    op.execute("UPDATE ingest_log SET outcome = 'created' WHERE outcome = 'review'")
    _rebuild_status_check(_STATUSES)


def downgrade() -> None:
    _rebuild_status_check(_WITH_REVIEW)


def _rebuild_status_check(allowed: str) -> None:
    # The indexes are rebuilt by hand on either side of the table copy: a partial index
    # does not survive SQLite reflection intact.
    op.drop_index("bookings_dedupe", table_name="bookings", sqlite_where=_DEDUPE_WHERE)
    op.drop_index("bookings_poll", table_name="bookings", sqlite_where=_POLL_WHERE)

    with op.batch_alter_table("bookings") as batch:
        batch.drop_constraint("bookings_status_check", type_="check")
        batch.create_check_constraint("bookings_status_check", allowed)

    op.create_index(
        "bookings_dedupe",
        "bookings",
        ["marketing_carrier", "marketing_number", "departure_local_date"],
        unique=True,
        sqlite_where=_DEDUPE_WHERE,
    )
    op.create_index("bookings_poll", "bookings", ["next_poll_at"], sqlite_where=_POLL_WHERE)
