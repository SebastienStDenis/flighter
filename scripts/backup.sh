#!/bin/sh
# Nightly pg_dump into the backups volume, newest 14 kept.
# Run from the host's crontab:
#   0 4 * * * docker compose -f /path/to/docker-compose.yml exec -T db /usr/local/bin/backup.sh
set -eu

DEST=/backups
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

mkdir -p "$DEST"
pg_dump -U flights -d flights --format=custom --file="$DEST/flights-$STAMP.dump"

# Keep two weeks. A personal flight history is small; the point is to survive a bad
# migration, not to archive forever.
ls -1t "$DEST"/flights-*.dump | tail -n +15 | xargs -r rm --
