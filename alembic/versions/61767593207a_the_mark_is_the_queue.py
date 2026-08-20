"""The mark is the queue.

The mail loop no longer walks a folder behind a UID cursor: it imports the messages you
have moved into the import mailbox and moves them out again when it is done, so the
`imap_cursor` row in `kv` names a position nothing reads any more.

There is nothing to restore on the way back down. The cursor was only ever a note of how
far a scan had got, and the code that wants one rebuilds it from a recent-mail scan the
first time it runs, exactly as it does on a fresh deployment.

The `imap_folder` preference needs no migration. Preferences are one JSONB blob validated
by a pydantic model, so the dead key is ignored on read, `imap_import_folder` falls back
to its default, and the first save from the settings page rewrites the blob without it.

Revision ID: 61767593207a
Revises: db14adca2741
Create Date: 2026-08-20 07:31:24.792056

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "61767593207a"
down_revision: str | None = "db14adca2741"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM kv WHERE key = 'imap_cursor'")


def downgrade() -> None:
    pass
