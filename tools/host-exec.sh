#!/usr/bin/env bash
#
# Run one plexdb command inside the deployed container, on the host.
#
# The container normally runs `plexdb schedule`, which sweeps on its own clock —
# the next sweep can be nine hours out. This is how a person runs one command
# against the live store *now*: after a deploy that fixes how the walk writes a
# row, the fix only reaches the store on the next walk, and waiting for the
# scheduler is not the same thing as choosing to repair.
#
# It runs the container's own `plexdb`, so the store is opened by the one writer
# through the mounts and credentials the container already has — never by a
# checkout on a laptop reaching across the network at a SQLite file.
#
# UNRAID_HOST and UNRAID_USER come from .env, so no hostname lands in a
# committed file. The container name matches [docker_run].container.
#
#   bash tools/host-exec.sh walk --section 2
#   bash tools/host-exec.sh sweep
#   bash tools/host-exec.sh check     # read-only; waits out a migration, never refuses

set -euo pipefail

: "${UNRAID_HOST:?set UNRAID_HOST in .env}"
TARGET="${UNRAID_USER:-root}@${UNRAID_HOST}"
CONTAINER="plexdb"

if [ "$#" -eq 0 ]; then
    echo "host-exec: give it a plexdb command, e.g. 'walk --section 2'" >&2
    exit 2
fi

# What counts as the container already writing. `migrate` rather than `init`:
# the command was renamed, and this pattern kept naming the old one, so a
# startup migration stopped matching and stopped being waited for.
BUSY="plexdb (sweep|walk|migrate)"

busy() {
    ssh "$TARGET" "docker exec $CONTAINER pgrep -f '$BUSY' >/dev/null"
}

# `check` opens the store read-only and writes nothing, so a sweep is no reason
# to refuse it — being able to ask a busy store how it is, is the point of it.
# It still waits out a *migration*, because a report taken halfway through one
# describes a store that no longer exists by the time it is printed. That is
# also what makes it usable as the last step of a deploy: the container migrates
# as it starts, and this waits for that to finish rather than racing it.
if [ "$1" = "check" ]; then
    waited=0
    while [ "$waited" -lt 180 ] && busy; do
        echo "host-exec: the container is busy, waiting (${waited}s)"
        sleep 5
        waited=$((waited + 5))
    done
elif busy; then
    # A second writer. SQLite would serialise them, but a walk racing a
    # migration is not something to find out about afterwards, so refuse.
    echo "host-exec: the container is mid-sweep; wait for it to finish" >&2
    exit 1
fi

echo "running 'plexdb $*' in $CONTAINER on $UNRAID_HOST"
exec ssh "$TARGET" "docker exec $CONTAINER plexdb $*"
