"""The store's schema version, and the procedure that applies a migration chain.

The store's own chain — every table `plexdb.db` holds — is defined in Rust, in
`crates/plexdb-schema`, and `plexdb migrate` runs it (`plexdb-rs migrate`). The
schema **is** the public API of this project (ADR-0001), and the writer and
`plexdb-reader` both compile against that one definition.

What stays here is what the Python half of the writer still needs: the version
and read shape a current store carries, which `plexdb check` compares a store
against, and `apply`, which `frames.py` runs over the Frame index's own chain
(ADR-0024). A test pins both numbers to the store the Rust chain builds.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from .errors import StoreError

#: A migration is either a batch of SQL or a step taking the connection, for
#: logic no SQL statement can express. `apply` below is what runs either kind.
Migration = str | Callable[[sqlite3.Connection], None]

#: The version a store is at once every migration in `crates/plexdb-schema` has
#: been applied (`plexdb_schema::SCHEMA_VERSION`).
SCHEMA_VERSION = 24

#: The read shape a fully migrated store carries in its `reader_shape` row
#: (`plexdb_schema::READER_SHAPE`), which `plexdb-reader`'s
#: `SUPPORTED_READER_SHAPE` must equal.
READER_SHAPE = 10


def current_version(conn: sqlite3.Connection) -> int:
    """The version this store is at. Zero means empty — nothing applied yet.

    Raises:
        StoreError: the store carries a `schema_version` table with no row. That
            is not an empty store — it is a damaged one, and treating it as
            empty would re-run migration 1 over tables that already exist and
            fail with a confusing "table already exists".
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if row is None:
        return 0
    version = conn.execute("SELECT version FROM schema_version").fetchone()
    if version is None:
        raise StoreError(
            "store has a schema_version table but no version row — it is damaged. "
            "Restore it from a backup, or delete the file and re-run `plexdb migrate` "
            "to rebuild it from Plex."
        )
    return int(version[0])


def apply(conn: sqlite3.Connection, migrations: tuple[Migration, ...]) -> tuple[int, int]:
    """Bring a file up to the last of `migrations`, and report where it started
    and ended.

    Applying an already-current file is a no-op, so this is safe to run on every
    open.

    Raises:
        StoreError: the file is newer than this code understands. That means a
            newer writer has touched it, and proceeding could corrupt rows this
            version cannot see.
    """
    target = len(migrations)
    start = current_version(conn)
    if start > target:
        raise StoreError(
            f"store is at schema version {start}, but this build only understands "
            f"{target} — upgrade plex-db-ex rather than writing with an older one"
        )
    if start == target:
        return start, start

    conn.executescript(
        "BEGIN;\nCREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);\nCOMMIT;\n"
    )

    # One transaction per step, around its DDL/data *and* the version row, so a
    # process killed mid-migration leaves the file at the last step that fully
    # landed rather than half-built. A SQL batch gets this via `executescript`
    # (whose BEGIN/COMMIT has to live inside the script text — `executescript`
    # commits any open transaction before it runs, so wrapping the call in
    # `with conn:` would not cover the DDL); a Python step gets it via an
    # explicit BEGIN/COMMIT around the call, rolled back on any exception.
    for index in range(start, target):
        migration = migrations[index]
        version = index + 1
        if isinstance(migration, str):
            conn.executescript(
                "BEGIN;\n"
                + migration
                + "\nDELETE FROM schema_version;\n"
                + f"INSERT INTO schema_version (version) VALUES ({version});\n"
                + "COMMIT;\n"
            )
        else:
            conn.execute("BEGIN")
            try:
                migration(conn)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            conn.execute("COMMIT")

    return start, target
