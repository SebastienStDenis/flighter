#!/bin/sh
# Nightly snapshot of the SQLite database, newest 14 kept.
# Run from the host's crontab:
#   0 4 * * * docker compose -f /path/to/examples/default/docker-compose.yml exec -T app /app/scripts/backup.sh
set -eu

DB=${DB:-/app/data/flighter.db}
DEST=${DEST:-/app/data/backups}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

mkdir -p "$DEST"
# VACUUM INTO, not cp: the app is running, and copying a file that is being written to
# gives a torn database rather than a backup. This takes a read lock, walks the pages,
# and writes a compacted copy that is consistent as of the moment it started.
python3 - "$DB" "$DEST/flighter-$STAMP.db" <<'PY'
import sqlite3
import sys

source, destination = sys.argv[1], sys.argv[2]
with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as connection:
    connection.execute("VACUUM INTO ?", (destination,))

# A copy nobody has read is a hope rather than a backup, and the one moment it is worth
# reading is now, while the database it came from is still there to take another.
with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as copy:
    verdict = copy.execute("PRAGMA integrity_check").fetchone()[0]
if verdict != "ok":
    sys.exit(f"backup {destination} is corrupt: {verdict}")
PY

# Keep two weeks. A personal flight history is small; the point is to survive a bad
# migration, not to archive forever.
ls -1t "$DEST"/flighter-*.db | tail -n +15 | xargs -r rm --
