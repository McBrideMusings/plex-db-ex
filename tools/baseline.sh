#!/usr/bin/env bash
#
# Copies of the live store, taken and moved without stopping anything.
#
# Every copy here goes through `VACUUM INTO` rather than `cp` or `rsync`. The
# store runs in WAL mode (plexdb/store.py), so rows that are committed can still
# be sitting in the `-wal` sidecar: a file copy taken while a sweep is running
# grabs the main database and leaves those rows behind, producing a file that
# opens cleanly and is quietly missing the last hour of work. `VACUUM INTO`
# reads through a normal connection, so it sees the sidecar and writes one
# self-contained file with no sidecars of its own.
#
#   pull    take a consistent copy on the host and bring it down to ./data
#   backup  take a copy on the host and leave it there, in the backups dir
#   list    show what backups the host is holding
#
# UNRAID_HOST, UNRAID_USER and PLEXDB_BASELINE_PATH come from .env, so no
# hostname or path lands in a committed file.

set -euo pipefail

: "${UNRAID_HOST:?set UNRAID_HOST in .env}"
: "${PLEXDB_BASELINE_PATH:?set PLEXDB_BASELINE_PATH in .env}"
TARGET="${UNRAID_USER:-root}@${UNRAID_HOST}"

# Where the container's own pre-migration copies land, so a manual backup sits
# beside them rather than in a second place nobody thinks to look.
BACKUP_DIR="$(dirname "$PLEXDB_BASELINE_PATH")/backups"
LOCAL_STORE="${PLEXDB_PATH:-./data/plexdb.db}"

stamp() { date -u +%Y%m%dT%H%M%SZ; }

remote_vacuum() {
    # $1 = destination path on the host. VACUUM INTO refuses to write over an
    # existing file, so a leftover from an interrupted run is cleared first.
    ssh "$TARGET" "rm -f '$1' && sqlite3 '$PLEXDB_BASELINE_PATH' \"VACUUM INTO '$1'\""
}

cmd_pull() {
    local remote="/tmp/plexdb-pull-$(stamp).db"
    echo "taking a consistent copy on $UNRAID_HOST..."
    remote_vacuum "$remote"
    mkdir -p "$(dirname "$LOCAL_STORE")"
    echo "copying it to $LOCAL_STORE..."
    scp "$TARGET:$remote" "$LOCAL_STORE"
    ssh "$TARGET" "rm -f '$remote'"
    # The sidecars belong to whatever was at this path before. Left in place
    # they describe a different database, which is how a restore corrupts.
    rm -f "$LOCAL_STORE-wal" "$LOCAL_STORE-shm"
    echo "done: $LOCAL_STORE"
    sqlite3 "$LOCAL_STORE" \
        "SELECT 'schema v' || (SELECT version FROM schema_version) ||
                ', ' || (SELECT count(*) FROM plays) || ' plays' ||
                ', ' || (SELECT count(*) FROM enrichment) || ' enrichment rows';"
}

cmd_backup() {
    local remote="$BACKUP_DIR/plexdb.manual-$(stamp).db"
    ssh "$TARGET" "mkdir -p '$BACKUP_DIR'"
    remote_vacuum "$remote"
    echo "backed up on $UNRAID_HOST: $remote"
    ssh "$TARGET" "ls -lh '$remote'"
}

cmd_list() {
    ssh "$TARGET" "ls -lh '$BACKUP_DIR' 2>/dev/null || echo 'no backups yet: $BACKUP_DIR'"
}

case "${1:-}" in
    pull)   cmd_pull ;;
    backup) cmd_backup ;;
    list)   cmd_list ;;
    *)      echo "usage: baseline.sh pull|backup|list" >&2; exit 2 ;;
esac
