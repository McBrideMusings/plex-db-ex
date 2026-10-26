"""Fixture stores, built by the writer's own migrate.

The store's schema is defined once, in `crates/plexdb-schema`, and `plexdb-rs
migrate` is what applies it. A test that needs a store runs that binary, never a
copy of its DDL, so a test can only pass against the tables the writer really
creates. `admin test` builds the binary and names it in `PLEXDB_RS_BIN`
(`tools/with-rs.sh`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

__all__ = ["init"]


def _binary() -> str:
    named = os.environ.get("PLEXDB_RS_BIN", "").strip() or "plexdb-rs"
    found = shutil.which(named)
    if found is None:
        raise RuntimeError(
            f"cannot build a fixture store: {named!r} not found. Run the tests with "
            "`admin test`, which builds plexdb-rs and sets PLEXDB_RS_BIN"
        )
    return found


def init(path: Path) -> None:
    """Create a store at `path` at the current schema, or bring an existing one
    up to it. Backups of an older store land in `backups/` beside it."""
    env = dict(os.environ, PLEXDB_PATH=str(path), PLEXDB_BACKUP_DIR=str(path.parent / "backups"))
    env.pop("PLEXDB_HELD_STORE", None)
    done = subprocess.run(
        [_binary(), "migrate"], env=env, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise RuntimeError(f"plexdb-rs migrate failed for {path}: {done.stderr.strip()}")
