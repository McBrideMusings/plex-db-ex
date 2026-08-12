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

set -euo pipefail

: "${UNRAID_HOST:?set UNRAID_HOST in .env}"
TARGET="${UNRAID_USER:-root}@${UNRAID_HOST}"
CONTAINER="plexdb"

if [ "$#" -eq 0 ]; then
    echo "host-exec: give it a plexdb command, e.g. 'walk --section 2'" >&2
    exit 2
fi

# A sweep already running would have this command writing to the store at the
# same time as the scheduler. SQLite would serialise them, but a walk racing a
# migration is not something to find out about afterwards, so refuse instead.
if ssh "$TARGET" "docker exec $CONTAINER pgrep -f 'plexdb (sweep|walk|init)' >/dev/null"; then
    echo "host-exec: the container is mid-sweep; wait for it to finish" >&2
    exit 1
fi

echo "running 'plexdb $*' in $CONTAINER on $UNRAID_HOST"
exec ssh "$TARGET" "docker exec $CONTAINER plexdb $*"
