"""Flights on iCloud Calendar rather than Google.

`bookings.gcal_event_id` held a Google Calendar event id; the column now holds the
iCalendar UID of the resource on iCloud, so it is renamed for what it carries.

The values themselves go. A Google event id names nothing on iCloud, and the column's
meaning is "this booking has been written to the calendar we sync to" - which, on the
first boot after this migration, is true of nothing. The Google entries are left where
they are: the app has no credentials for that account any more, and clearing them out is
a job for the person who owns it.

Both directions are lossy in the same way and cannot be otherwise, since neither id can
be computed from the other.

The `gcal_calendar_id` preference needs no migration. Preferences are one JSONB blob
validated by a pydantic model, so the dead key is ignored on read, `icloud_calendar_name`
falls back to its default, and the first save from the settings page rewrites the blob
without it.

Revision ID: db14adca2741
Revises: 3ac36377b965
Create Date: 2026-08-20 07:05:24.621262

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "db14adca2741"
down_revision: str | None = "3ac36377b965"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("bookings", "gcal_event_id", new_column_name="calendar_event_uid")
    op.execute("UPDATE bookings SET calendar_event_uid = NULL")


def downgrade() -> None:
    op.alter_column("bookings", "calendar_event_uid", new_column_name="gcal_event_id")
    op.execute("UPDATE bookings SET gcal_event_id = NULL")
