#!/usr/bin/env bash
# Build plexdb-rs, point PLEXDB_RS_BIN at the build, then run the rest of the
# command line. `admin dev` and `admin parity` run the Python CLI through this, so
# a sweep step that has moved to Rust always runs the code in this checkout.
#
#   bash tools/with-rs.sh [--release] <command> [args...]
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
profile=debug
flags=()
if [[ "${1:-}" == "--release" ]]; then
  profile=release
  flags=(--release)
  shift
fi
cargo build -q "${flags[@]}" --manifest-path "$repo/Cargo.toml" -p plexdb --bin plexdb-rs
target="${CARGO_TARGET_DIR:-$repo/target}"
export PLEXDB_RS_BIN="$target/$profile/plexdb-rs"
exec "$@"
