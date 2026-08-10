"""Opening the store.

One process writes this file and everything else reads it, so the read-only
path is a real mode here rather than a convention — a reader that *can* write is
a bug waiting for a caller.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import schema
from .errors import StoreError

# A write batch — one BEGIN IMMEDIATE ... COMMIT, e.g. one page of walk() upserts
# — holds the write lock for well under a second; an enrichment sweep spans
# minutes but as many such batches, never one held transaction (ADR-0007). 5000ms
# gives a second writer generous room to wait out one batch instead of failing
# immediately, while still surfacing a genuinely stuck writer within a few
# seconds. This is also the value `sqlite3.connect`'s own default (timeout=5.0)
# already produced; stating it here turns that into a choice instead of an
# inherited default.
BUSY_TIMEOUT_MS = 5000


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    # WAL lets a reader work while the writer is mid-sweep, which is the normal
    # state of this store: one long enrichment pass, several consumers reading.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")


def _ensure_parent(path: Path) -> None:
    """Make the directory `path` will live in, reporting failure as `StoreError`."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        raise StoreError(f"cannot create {path.parent}: {err.strerror}") from err


@contextmanager
def open_store(path: Path, *, create: bool = False) -> Iterator[sqlite3.Connection]:
    """Open the store for writing.

    Args:
        path: where the store lives.
        create: create the file and its parent directory if absent. Off by
            default so a typo'd path fails loudly instead of silently making a
            second, empty store.
    """
    if path.is_dir():
        raise StoreError(f"{path} is a directory, not a store — point PLEXDB_PATH at a file")
    if create:
        _ensure_parent(path)
    elif not path.exists():
        raise FileNotFoundError(f"no store at {path} — run `plexdb init` first")

    conn = _connect(path)
    try:
        yield conn
    finally:
        conn.close()


def _connect(path: Path) -> sqlite3.Connection:
    """Open and configure a connection, reporting failures as `StoreError`.

    `sqlite3.connect` is lazy — pointing it at a text file succeeds and the
    failure only surfaces on the first statement. So the configuration PRAGMAs
    double as the check that this file is really a database.
    """
    try:
        conn = sqlite3.connect(path)
    except sqlite3.Error as err:
        raise StoreError(f"cannot open {path}: {err}") from err
    try:
        _configure(conn)
    except sqlite3.DatabaseError as err:
        conn.close()
        raise StoreError(f"{path} is not a plexdb store: {err}") from err
    return conn


@contextmanager
def open_readonly(path: Path) -> Iterator[sqlite3.Connection]:
    """Open the store read-only. Any write raises rather than being applied."""
    if not path.exists():
        raise FileNotFoundError(f"no store at {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as err:
        raise StoreError(f"cannot open {path} read-only: {err}") from err
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        # Touch the store before handing it over, so a file that is not a
        # database — or a WAL store whose directory is not writable — fails here
        # as a StoreError rather than leaking a raw sqlite3 error into a caller
        # several frames away.
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.DatabaseError as err:
        conn.close()
        raise StoreError(f"cannot read {path}: {err}") from err
    try:
        yield conn
    finally:
        conn.close()


def init(path: Path) -> tuple[int, int]:
    """Create the store if absent and bring its schema up to date.

    Returns the version it was at and the version it is now, so a caller can
    tell "created" from "already current" without inspecting the file.
    """
    with open_store(path, create=True) as conn:
        return schema.apply(conn)


def publish(store_path: Path, snapshot_path: Path) -> tuple[int, int]:
    """Publish a read-only snapshot of the store for consumers (ADR-0007).

    `VACUUM INTO` writes a compacted, self-contained copy of the store with no
    `-wal` or `-shm` sidecars — confirmed empirically: a database vacuumed out
    of a WAL-mode source lands in SQLite's default `DELETE` journal mode
    regardless of the source's mode, which is what lets the copy be opened
    read-only from a directory with no write permission.

    The copy is written under a temporary name beside `snapshot_path` and then
    renamed into place. A rename within one filesystem is atomic and a handle
    already open on the old file keeps reading it, so a consumer opening
    `snapshot_path` never observes a half-written file, and a second publish
    replaces the snapshot rather than erroring.

    Returns the schema version and the byte size of the file written.
    """
    _ensure_parent(snapshot_path)

    tmp_path = snapshot_path.with_name(snapshot_path.name + ".tmp")
    # VACUUM INTO refuses to write over an existing file, so clear one left
    # behind by a publish that crashed mid-write before starting a new one.
    tmp_path.unlink(missing_ok=True)

    with open_store(store_path) as conn:
        try:
            conn.execute("VACUUM INTO ?", (str(tmp_path),))
        except sqlite3.Error as err:
            tmp_path.unlink(missing_ok=True)
            raise StoreError(f"cannot publish snapshot: {err}") from err
        version = schema.current_version(conn)

    tmp_path.replace(snapshot_path)
    return version, snapshot_path.stat().st_size
