"""Bounded retries, and the calendar picked by URL.

`ingest_log` gains the three columns a bounded retry needs: how many attempts a message
has had, when the next one is due, and the subject line, because the set-aside list on
the health page has to say which email it is talking about and a Message-ID names nothing
a person recognises.

Rows written before this arrive with no attempts and no retry time. An `error` row among
them therefore reads as set aside, which is the safe way round: the message is still
flagged in Mail, and one press of Try again puts it back in the queue.

The calendar preference needs no migration. Preferences are one JSONB blob validated by a
pydantic model, so the old `icloud_calendar_name` key is ignored on read,
`icloud_calendar_url` falls back to its default of nothing, and picking the calendar again
on the settings page rewrites the blob without it.

Revision ID: 59bad4bddcea
Revises: 61767593207a
Create Date: 2026-08-20 08:38:51.700183

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "59bad4bddcea"
down_revision: str | None = "61767593207a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ingest_log",
        sa.Column("subject", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "ingest_log",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "ingest_log",
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ingest_log", "retry_at")
    op.drop_column("ingest_log", "attempts")
    op.drop_column("ingest_log", "subject")
