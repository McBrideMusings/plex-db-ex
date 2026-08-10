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


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    # WAL lets a reader work while the writer is mid-sweep, which is the normal
    # state of this store: one long enrichment pass, several consumers reading.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")


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
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            raise StoreError(f"cannot create {path.parent}: {err.strerror}") from err
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
