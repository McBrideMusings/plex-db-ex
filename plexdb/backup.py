"""Copies of the store taken before anything changes its schema.

The store holds twenty months of watch history and a rate-limited TMDB sweep,
neither of which a re-scan reproduces. `schema.apply` already wraps every
pending migration and the version row in one transaction, so a process killed
partway through leaves the file untouched — but that protects against a *crash*,
not against a migration that is simply wrong. Bad SQL commits perfectly happily.

So a copy is taken first, and it is kept.

**Nothing this module writes is ever pruned.** Every copy taken here is a
pre-migration one, and the two kinds of copy in the backups directory are not
worth the same. A `plexdb.pre-v<N>.db` is
the only route back past migration N, there is at most one per schema version,
and the list of versions grows by one every few weeks — so the whole set is
bounded by the schema's own history and none of it is redundant. A
`plexdb.manual-<timestamp>.db` is a copy someone took before touching something,
and the tenth-oldest of those is a duplicate of a store that has since been
migrated twice; at ~130 MB each they are what actually fills a disk. Those are
taken on the host and pruned there, by `tools/baseline.sh` — nothing in this
package deletes a backup.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .errors import StoreError

#: Tables whose contents no re-run reproduces, checked for row loss after a
#: migration. `plays` is watch history Tautulli eventually forgets, `enrichment`
#: is a TMDB sweep paid for against a rate limit, and `items` is what both hang
#: off. Everything else in the schema — edges, collections, cursors — is
#: re-fetchable, so a migration that rebuilds one of those is not a red flag.
GUARDED_TABLES = ("items", "plays", "enrichment")


@dataclass(frozen=True)
class Backup:
    """A copy of the store on disk, and what was in it when it was taken."""

    path: Path
    #: Row counts for `GUARDED_TABLES`, for comparison after a migration.
    counts: dict[str, int]
    size: int


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


def _unique_path(directory: Path, stem: str) -> Path:
    """`<stem>.db`, or `<stem>.2.db` if that name is taken.

    A migration can be attempted, rolled back, and attempted again — all at the
    same target version, so all wanting the same name. The second attempt must
    not overwrite the copy taken before the first one; that copy is the older
    and therefore the more valuable of the two.
    """
    candidate = directory / f"{stem}.db"
    attempt = 2
    while candidate.exists():
        candidate = directory / f"{stem}.{attempt}.db"
        attempt += 1
    return candidate


def take(store_path: Path, backup_dir: Path, label: str) -> Backup:
    """Write a self-contained copy of the store into `backup_dir`.

    Uses `VACUUM INTO`, the same mechanism `store.publish` uses: it produces one
    file with no `-wal` or `-shm` sidecars, and it reads through a normal
    connection, so a copy taken while another process is mid-write still lands
    consistent. A plain file copy does neither — the store runs in WAL mode, so
    committed rows can be sitting in the sidecar a `cp` leaves behind.
    """
    if not store_path.exists():
        raise StoreError(f"cannot back up a store that does not exist: {store_path}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = _unique_path(backup_dir, label)

    conn = sqlite3.connect(store_path)
    try:
        counts = guarded_counts(conn)
        conn.execute("VACUUM INTO ?", (str(destination),))
    except sqlite3.Error as err:
        destination.unlink(missing_ok=True)
        raise StoreError(f"cannot back up {store_path}: {err}") from err
    finally:
        conn.close()

    return Backup(path=destination, counts=counts, size=destination.stat().st_size)


def restore(backup: Path, store_path: Path) -> None:
    """Put a backup back, replacing the store.

    The `-wal` and `-shm` sidecars are removed rather than left: they describe
    the file being replaced, and SQLite reading a stale sidecar next to a
    different database is how a restore turns into a corruption.
    """
    if not backup.exists():
        raise StoreError(f"cannot restore from a backup that does not exist: {backup}")

    shutil.copyfile(backup, store_path)
    for sidecar in (f"{store_path}-wal", f"{store_path}-shm"):
        Path(sidecar).unlink(missing_ok=True)
