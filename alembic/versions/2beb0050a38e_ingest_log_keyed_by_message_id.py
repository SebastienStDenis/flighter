"""The ingest log, keyed on the RFC822 Message-ID.

A rename only. Rows written before it keep whatever id they were recorded under, which
simply never matches a Message-ID again: the worst that costs is one email looked at a
second time, and the booking dedupe is what stops that becoming a second flight.

Revision ID: 2beb0050a38e
Revises: 36aa9409ff82
Create Date: 2026-08-19 21:32:08.439096

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "2beb0050a38e"
down_revision: str | None = "36aa9409ff82"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("ingest_log", "gmail_message_id", new_column_name="message_id")


def downgrade() -> None:
    op.alter_column("ingest_log", "message_id", new_column_name="gmail_message_id")
