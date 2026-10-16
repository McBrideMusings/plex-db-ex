"""The Frame index: `frames.db`, a second SQLite file beside the store (ADR-0024).

It holds every title's Cuts and one CLIP vector per Shot. It is derived — the
frame harvester rebuilds a lost file from the media through Plex — and at full
library it runs to tens of gigabytes, so it is never snapshotted or backed up,
and it has its own schema version and migration chain, applied by the same
`schema.apply` the store uses.

Two rules shape every write here:

- **Rollback-journal mode, not WAL.** A reader opens the file read-only where
  it lies and `ATTACH`es it to the Snapshot; with no `-wal`/`-shm` sidecars it
  needs no write access to the directory to do that.
- **No transaction touches both files.** Whatever this module needs from
  `plexdb.db` — which item_ids exist, which section each sits in — it reads
  through a separate read-only connection first, then writes `frames.db` alone.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any

import sqlite_vec

from . import schema
from .config import Config
from .errors import ConfigError, PlexdbError, StoreError
from .store import BUSY_TIMEOUT_MS

__all__ = [
    "FRAME_DIMS",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "PruneStats",
    "UnknownItem",
    "frames_path",
    "inside_hours",
    "migrate",
    "migrate_configured",
    "open_frames",
    "parse_hours",
    "prune_orphans",
    "queue",
    "status",
]

#: Width of one frame vector: CLIP ViT-L/14's projection, the model the encoder
#: serves (ADR-0025). vec0 fixes a column's width when the table is created, so
#: a model of another width is a migration, not a setting.
FRAME_DIMS = 768

#: Version 1 — the four tables the Frame search epic names.
_V1 = f"""
-- One row per title the harvester has attempted. `error` is the last failure's
-- reason, and NULL once the title indexed cleanly. `file_hash` is the part's
-- size + mtime + first MiB; a different hash means the file was replaced and
-- the title is indexed again.
CREATE TABLE frame_index_state (
    item_id       TEXT PRIMARY KEY,
    part_id       INTEGER,
    model         TEXT,
    model_version TEXT,
    dims          INTEGER,
    quant         TEXT,
    duration_ms   INTEGER,
    bytes         INTEGER,
    file_hash     TEXT,
    shots         INTEGER,
    vectors       INTEGER,
    indexed_at    TEXT NOT NULL,
    error         TEXT
);

-- One vector per Shot, plus one every 2 s inside a longer Shot. Partitioned by
-- item_id, so a search over a pool of titles reads only those titles' chunks.
CREATE VIRTUAL TABLE frame_vec USING vec0(
    item_id TEXT PARTITION KEY,
    t_ms    INTEGER,
    shot_ix INTEGER,
    vec     int8[{FRAME_DIMS}]
);

-- Shot boundaries: scdet's scene changes, and the file's chapter marks.
CREATE TABLE cuts (
    item_id TEXT NOT NULL,
    t_ms    INTEGER NOT NULL,
    score   REAL,
    source  TEXT NOT NULL CHECK (source IN ('scdet', 'chapter')),
    PRIMARY KEY (item_id, source, t_ms)
);

-- Titles a person asked for ahead of the backlog, served oldest first.
CREATE TABLE frame_queue (
    item_id   TEXT PRIMARY KEY,
    queued_at TEXT NOT NULL,
    queued_by TEXT NOT NULL
);
"""

#: Append-only, like `schema.MIGRATIONS`, and versioned separately from it.
MIGRATIONS: tuple[schema.Migration, ...] = (_V1,)
SCHEMA_VERSION = len(MIGRATIONS)

#: Every frames.db table keyed by item_id, in the order a prune deletes from.
_TITLE_TABLES = ("frame_vec", "cuts", "frame_queue", "frame_index_state")


class UnknownItem(PlexdbError):
    """An item_id the store's `items` table does not hold."""


def frames_path(config: Config) -> Path:
    """Where `frames.db` lives, or a `ConfigError` when nobody configured it."""
    if config.frames_path is None:
        raise ConfigError("PLEXDB_FRAMES_DIR is not set, so there is no Frame index")
    return config.frames_path


def _connect(path: Path) -> sqlite3.Connection:
    if path.is_dir():
        raise StoreError(f"{path} is a directory, not a frames.db")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        raise StoreError(f"cannot create {path.parent}: {err.strerror}") from err
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    except sqlite3.DatabaseError as err:
        conn.close()
        raise StoreError(f"{path} is not a frames.db: {err}") from err
    return conn


@contextmanager
def open_frames(path: Path) -> Iterator[sqlite3.Connection]:
    """Open `frames.db` for writing, creating it and applying any pending
    migration first. Opening a current file changes nothing, so every caller
    may open it this way."""
    conn = _connect(path)
    try:
        schema.apply(conn, MIGRATIONS)
        yield conn
    finally:
        conn.close()


def migrate(path: Path) -> tuple[int, int]:
    """Bring `frames.db` to `SCHEMA_VERSION`, creating it if absent. Returns the
    version it was at and the one it is at now.

    No copy is taken first: the file is rebuildable and too large to copy
    (ADR-0024), and each step runs in its own transaction, so a failure leaves
    the last step that fully landed.
    """
    conn = _connect(path)
    try:
        was, now, _ = schema.apply(conn, MIGRATIONS)
    finally:
        conn.close()
    return was, now


def migrate_configured(config: Config) -> str:
    """Migrate `frames.db` when the Frame index is configured, and say what
    happened in one line. `plexdb frames migrate` and the container's startup
    migration both call this. `plexdb migrate` does not: it is a required sweep
    Step, and the optional Frame index must not be able to stop a sweep."""
    if config.frames_path is None:
        return "frames.db: not configured (PLEXDB_FRAMES_DIR is unset)"
    path = config.frames_path
    was, now = migrate(path)
    if was == now:
        return f"frames.db already current at schema v{now}: {path.resolve()}"
    if was == 0:
        return f"created frames.db at schema v{now}: {path.resolve()}"
    return f"migrated frames.db v{was} -> v{now}: {path.resolve()}"


def parse_hours(raw: str) -> tuple[time, time] | None:
    """`"01:00-07:00"` -> `(01:00, 07:00)`; empty -> `None`. A window whose end
    is earlier than its start runs past midnight."""
    if not raw:
        return None
    try:
        start_raw, end_raw = raw.split("-")
        start, end = time.fromisoformat(start_raw.strip()), time.fromisoformat(end_raw.strip())
    except ValueError as err:
        raise ConfigError(f"PLEXDB_FRAME_HOURS must look like 01:00-07:00, got {raw!r}") from err
    if start.tzinfo is not None or end.tzinfo is not None:
        raise ConfigError(
            f"PLEXDB_FRAME_HOURS takes wall-clock times in the process's TZ, no offset: {raw!r}"
        )
    if start == end:
        raise ConfigError(f"PLEXDB_FRAME_HOURS opens and closes at the same time: {raw!r}")
    return start, end


def inside_hours(window: tuple[time, time] | None, now: datetime) -> bool:
    """Whether `now`'s wall-clock time falls in `window`. No window, never."""
    if window is None:
        return False
    start, end = window
    at = now.time()
    return start <= at < end if start < end else at >= start or at < end


def _section_titles(store_conn: sqlite3.Connection) -> dict[str, set[str]]:
    """The movies and episodes in each Plex section — the titles the harvester
    indexes. A show has no file of its own."""
    rows = store_conn.execute(
        "SELECT DISTINCT p.section_id, p.item_id FROM plex_items p "
        "JOIN items i ON i.item_id = p.item_id WHERE i.type IN ('movie', 'episode')"
    ).fetchall()
    sections: dict[str, set[str]] = {}
    for row in rows:
        sections.setdefault(str(row[0]), set()).add(str(row[1]))
    return sections


def _section_order(section_id: str) -> tuple[int, int, str]:
    return (0, int(section_id), "") if section_id.isdigit() else (1, 0, section_id)


def status(
    frames_conn: sqlite3.Connection,
    store_conn: sqlite3.Connection,
    *,
    path: Path,
    hours: str,
    now: datetime,
) -> dict[str, Any]:
    """What `plexdb frames status` prints. Its shape is documented in
    docs/schema.md; a key added here goes there too."""
    indexed = {
        str(row[0])
        for row in frames_conn.execute(
            "SELECT item_id FROM frame_index_state WHERE error IS NULL"
        ).fetchall()
    }
    sections = _section_titles(store_conn)
    failed = frames_conn.execute(
        "SELECT item_id, error, indexed_at FROM frame_index_state "
        "WHERE error IS NOT NULL ORDER BY indexed_at DESC, item_id"
    ).fetchall()
    queued = int(frames_conn.execute("SELECT count(*) FROM frame_queue").fetchone()[0])
    return {
        "frames_db": str(path.resolve()),
        "schema_version": schema.current_version(frames_conn),
        "bytes": path.stat().st_size,
        "sections": [
            {
                "section_id": section_id,
                "indexed": len(titles & indexed),
                "total": len(titles),
            }
            for section_id, titles in sorted(
                sections.items(), key=lambda pair: _section_order(pair[0])
            )
        ],
        "queued": queued,
        "frame_hours": hours or None,
        "inside_hours": inside_hours(parse_hours(hours), now),
        # The harvester and the encoder client arrive in later slices; until
        # then nothing is being indexed and nothing has asked the encoder.
        "indexing": None,
        "encoder_reachable": None,
        "failed": [
            {"item_id": row["item_id"], "error": row["error"], "at": row["indexed_at"]}
            for row in failed
        ],
    }


def queue(
    frames_conn: sqlite3.Connection,
    item_id: str,
    *,
    known: bool,
    by: str,
    now: datetime,
) -> bool:
    """Put `item_id` at the back of the queue. Returns whether it was added;
    a title already queued keeps its place.

    `known` is whether the store's `items` holds the id, read by the caller
    through its own read-only connection so no transaction spans both files.
    """
    if not known:
        raise UnknownItem(f"{item_id} is not in items — nothing to queue")
    with frames_conn:
        cursor = frames_conn.execute(
            "INSERT OR IGNORE INTO frame_queue (item_id, queued_at, queued_by) VALUES (?, ?, ?)",
            (item_id, now.isoformat(), by),
        )
    return cursor.rowcount == 1


@dataclass(frozen=True)
class PruneStats:
    """What one prune removed, per table, and for which titles."""

    item_ids: tuple[str, ...]
    deleted: dict[str, int] = field(default_factory=dict)


def _titles_held(frames_conn: sqlite3.Connection) -> set[str]:
    """Every item_id frames.db holds a row for.

    Read from the three ordinary tables, never by scanning `frame_vec`, which
    at full library is tens of millions of rows. The harvester writes a title's
    vectors and its `frame_index_state` row in one transaction, so a title with
    vectors always has a state row.
    """
    held: set[str] = set()
    for table in ("frame_index_state", "cuts", "frame_queue"):
        held.update(
            str(row[0])
            for row in frames_conn.execute(f"SELECT DISTINCT item_id FROM {table}")  # noqa: S608
        )
    return held


def prune_orphans(frames_conn: sqlite3.Connection, live: set[str]) -> PruneStats:
    """Delete every frames.db row whose item_id is not in `live` — the store's
    `items`, read by the caller. One transaction per title, matching the
    harvester's one commit per title."""
    orphans = sorted(_titles_held(frames_conn) - live)
    deleted = dict.fromkeys(_TITLE_TABLES, 0)
    for item_id in orphans:
        with frames_conn:
            for table in _TITLE_TABLES:
                cursor = frames_conn.execute(
                    f"DELETE FROM {table} WHERE item_id = ?",  # noqa: S608
                    (item_id,),
                )
                deleted[table] += cursor.rowcount
    return PruneStats(item_ids=tuple(orphans), deleted=deleted)
