"""Row counts for the tables a rewrite must not lose rows from.

The store holds twenty months of watch history and a rate-limited TMDB sweep,
neither of which a re-scan reproduces. `plexdb check` reads these counts. The copies
taken before a migration (`plexdb.pre-v<N>.db`) or before `repair-fs-identities`
(`plexdb.pre-repair-fs-identities.db`) are taken in Rust
(`crates/plexdb/src/migrate.rs`) into `backups/` beside the store, and nothing in
this package deletes one: a `pre-v<N>` copy is the only route back past migration N.
Manual copies are taken on the host and pruned there, by `tools/baseline.sh`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

#: Tables whose contents no re-run reproduces, checked for row loss after a
#: rewrite (`plexdb_schema::GUARDED_TABLES`, which `plexdb migrate` checks).
#: `plays` is watch history Tautulli eventually forgets, `enrichment` is a TMDB
#: sweep paid for against a rate limit, and `items` is what both hang off.
GUARDED_TABLES = ("items", "plays", "enrichment")


def counts_for(conn: sqlite3.Connection, tables: Sequence[str]) -> dict[str, int]:
    """Row counts for every named table that exists yet, in the order given.

    A table missing from the store is left out rather than counted as zero:
    before the migration that creates it, absent and empty are the same thing,
    and reporting `0` would make the migration that fills it look like growth
    from nothing when it was creation. `plexdb check` runs against stores older
    than this build for the same reason, so it wants the same treatment.
    """
    present = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    counts: dict[str, int] = {}
    for table in tables:
        if table in present:
            counts[table] = int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    return counts


def guarded_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row counts for every table in `GUARDED_TABLES` that exists yet."""
    return counts_for(conn, GUARDED_TABLES)
