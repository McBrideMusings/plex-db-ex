#!/usr/bin/env bash
# Print the last N lines of the container's log on the host and exit.
#
# `admin logs live` follows the stream and never returns, which is the right
# shape for a person watching a sweep and the wrong shape for anything that
# needs an answer — a script, a check after a deploy, an agent. This is the
# bounded read: N lines, then done.
#
# UNRAID_HOST and UNRAID_USER come from .env, same as tools/baseline.sh, so no
# hostname appears in a committed file.
set -euo pipefail

cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a

: "${UNRAID_HOST:?set UNRAID_HOST in .env}"
TARGET="${UNRAID_USER:-root}@${UNRAID_HOST}"
CONTAINER="${PLEXDB_CONTAINER:-plexdb}"
LINES="${1:-80}"

case "$LINES" in
    ''|*[!0-9]*) echo "usage: $(basename "$0") [line-count]" >&2; exit 2 ;;
esac

echo "last $LINES line(s) of '$CONTAINER' on $UNRAID_HOST:"
ssh "$TARGET" "docker logs --tail $LINES '$CONTAINER' 2>&1"
