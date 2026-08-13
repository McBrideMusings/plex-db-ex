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

# How many manual copies to keep. A `plexdb.pre-v<N>.db` is never pruned — it is
# the only route back past migration N, and there is at most one per schema
# version — but a manual copy is one somebody took before touching something,
# and the tenth-oldest is a duplicate of a store that has been migrated twice
# since. At ~130 MB each they are what fills the disk.
MANUAL_KEEP=10

prune_manual() {
    # Newest first, skip the first MANUAL_KEEP, delete the rest. Name-sorted is
    # date-sorted: the stamp is UTC `YYYYMMDDTHHMMSSZ`, so it sorts the same way
    # it reads, with no dependency on the host's mtimes surviving a copy.
    ssh "$TARGET" "ls -1 '$BACKUP_DIR'/plexdb.manual-*.db 2>/dev/null | sort -r | tail -n +$((MANUAL_KEEP + 1)) | while read -r old; do echo \"pruned \$old\"; rm -f \"\$old\"; done"
}

cmd_backup() {
    # `backup` is also the first step of `admin deploy`, which forwards its own
    # flags here. A preview must not leave a 136 MB file on the host.
    if [ "${1:-}" = "--dry-run" ]; then
        echo "dry run: would copy the store into $BACKUP_DIR/plexdb.manual-<stamp>.db"
        return 0
    fi
    local remote="$BACKUP_DIR/plexdb.manual-$(stamp).db"
    ssh "$TARGET" "mkdir -p '$BACKUP_DIR'"
    remote_vacuum "$remote"
    echo "backed up on $UNRAID_HOST: $remote"
    ssh "$TARGET" "ls -lh '$remote'"
    prune_manual
}

cmd_list() {
    ssh "$TARGET" "ls -lh '$BACKUP_DIR' 2>/dev/null || echo 'no backups yet: $BACKUP_DIR'"
}

case "${1:-}" in
    pull)   cmd_pull ;;
    backup) shift; cmd_backup "$@" ;;
    list)   cmd_list ;;
    *)      echo "usage: baseline.sh pull|backup|list" >&2; exit 2 ;;
esac
