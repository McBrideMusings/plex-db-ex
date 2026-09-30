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
#   bash tools/host-exec.sh check     # read-only; never refuses

set -euo pipefail

: "${UNRAID_HOST:?set UNRAID_HOST in .env}"
TARGET="${UNRAID_USER:-root}@${UNRAID_HOST}"
CONTAINER="plexdb"

if [ "$#" -eq 0 ]; then
    echo "host-exec: give it a plexdb command, e.g. 'walk --section 2'" >&2
    exit 2
fi

# `check` opens the store read-only and writes nothing, so a sweep is no reason
# to refuse it — being able to ask a busy store how it is, is the point of it.
# Waiting out the container's startup migration is `plexdb check --wait`'s job:
# it polls the store's schema version, which is what the migration commits.
#
# Anything else runs as `plexdb idle <command>`, which takes the store's locks
# and runs the command only if no migration or writer held them. The startup
# migration and the nightly sweep both run inside `plexdb schedule`, so no
# process name gives them away; the locks do. Checking and running happen in one
# process under one claim, so a sweep cannot start between them. A busy store
# prints why and exits 1, and an ssh or docker failure is non-zero too.
echo "running 'plexdb $*' in $CONTAINER on $UNRAID_HOST"
if [ "$1" = "check" ]; then
    exec ssh "$TARGET" "docker exec $CONTAINER plexdb $*"
fi
exec ssh "$TARGET" "docker exec $CONTAINER plexdb idle $*"
