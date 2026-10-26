"""The Frame index: `frames.db`, a second SQLite file beside the store (ADR-0024).

It holds every title's Cuts and one CLIP vector per Shot. It is derived — the
frame harvester rebuilds a lost file from the media through Plex — and at full
library it runs to tens of gigabytes, so it is never snapshotted or backed up,
and it has its own schema version and migration chain, applied by
`schema.apply`.

Two rules shape every write here:

- **Rollback-journal mode, not WAL.** A reader opens the file read-only where
  it lies and `ATTACH`es it to the Snapshot; with no `-wal`/`-shm` sidecars it
  needs no write access to the directory to do that.
- **No transaction touches both files.** Whatever this module needs from
  `plexdb.db` — which item_ids exist, which section each sits in — it reads
  through a separate read-only connection first, then writes `frames.db` alone.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import struct
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, time
from pathlib import Path
from typing import IO, Any

import sqlite_vec

from . import schema
from .config import Config
from .errors import ConfigError, PlexdbError, StoreError
from .store import BUSY_TIMEOUT_MS

__all__ = [
    "FRAME_DIMS",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "UnknownItem",
    "frames_path",
    "harvester_running",
    "inside_hours",
    "migrate",
    "migrate_configured",
    "open_frames",
    "parse_hours",
    "queue",
    "section_titles",
    "status",
]

#: `struct flock` as `fcntl` takes it. The field order differs between the BSD and
#: Linux layouts; both leave `l_start` and `l_len` zero, which means the whole file.
_FLOCK_DARWIN = sys.platform == "darwin"
_FLOCK_FORMAT = "qqihh" if _FLOCK_DARWIN else "hhqqi"

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

#: Version 2 — the title the harvester is indexing now, so `plexdb frames status`,
#: a separate process, can say which. At most one row.
_V2 = """
CREATE TABLE frame_harvest (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    item_id  TEXT NOT NULL,
    since    TEXT NOT NULL
);
"""


def _V3(conn: sqlite3.Connection) -> None:
    """Version 3 — each Shot's boundaries, stored by the harvester so a reader
    never re-derives them from `cuts`. The titles already indexed get their rows
    here, from the rule the harvester applied when it numbered them: the boundaries
    are 0, each distinct cut at least 500 ms after the boundary before it and before
    `duration_ms` (the last frame's offset when that is NULL), then the end. The rule is
    written out in this step, not imported, so a later change to the harvester's rule
    leaves this one as it ran."""
    conn.execute(
        """
        CREATE TABLE shots (
            item_id TEXT NOT NULL,
            shot_ix INTEGER NOT NULL,
            in_ms   INTEGER NOT NULL,
            out_ms  INTEGER NOT NULL,
            PRIMARY KEY (item_id, shot_ix)
        ) WITHOUT ROWID
        """
    )
    titles = conn.execute(
        "SELECT item_id, duration_ms FROM frame_index_state WHERE model IS NOT NULL"
    ).fetchall()
    for item_id, duration_ms in titles:
        if duration_ms is None:
            duration_ms = conn.execute(
                "SELECT max(t_ms) FROM frame_vec WHERE item_id = ?", (item_id,)
            ).fetchone()[0]
        if duration_ms is None:
            continue
        bounds = [0]
        for (t_ms,) in conn.execute(
            "SELECT DISTINCT t_ms FROM cuts WHERE item_id = ? ORDER BY t_ms", (item_id,)
        ):
            if 500 <= t_ms - bounds[-1] and t_ms < duration_ms:
                bounds.append(t_ms)
        bounds.append(duration_ms)
        conn.executemany(
            "INSERT INTO shots (item_id, shot_ix, in_ms, out_ms) VALUES (?, ?, ?, ?)",
            [
                (item_id, ix, start, end)
                for ix, (start, end) in enumerate(zip(bounds, bounds[1:], strict=False))
            ],
        )


#: Version 4 — `reason`, the short path-free phrase for a failed title, beside `error`'s
#: full message. Titles already failed keep NULL: their message is not rewritten.
_V4 = """
ALTER TABLE frame_index_state ADD COLUMN reason TEXT;
"""

#: Append-only, like the store's chain, and versioned separately from it.
MIGRATIONS: tuple[schema.Migration, ...] = (_V1, _V2, _V3, _V4)
SCHEMA_VERSION = len(MIGRATIONS)


class UnknownItem(PlexdbError):
    """An item_id the store's `items` table does not hold."""


def frames_path(config: Config) -> Path:
    """Where `frames.db` lives, or a `ConfigError` when nobody configured it."""
    if config.frames_path is None:
        raise ConfigError("PLEXDB_FRAMES_DIR is not set, so there is no Frame index")
    return config.frames_path


def decoder_path(frames_file: Path) -> Path:
    return frames_file.parent / "decoder.json"


def decoder_report(frames_file: Path, requested: str) -> str:
    """What `plexdb frames status` shows as `decoder`: the requested decoder, or
    `cpu (cuda requested: <reason>)` after the harvester fell back."""
    if requested == "cpu":
        return "cpu"
    try:
        reason = json.loads(decoder_path(frames_file).read_text())["cuda_failed"]
    except (OSError, ValueError, KeyError):
        return requested
    return f"cpu (cuda requested: {reason})"


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
        was, now = schema.apply(conn, MIGRATIONS)
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


def section_titles(store_conn: sqlite3.Connection) -> dict[str, set[str]]:
    """The movies and episodes in each Plex section — the titles the harvester
    indexes. A show has no file of its own."""
    sections: dict[str, set[str]] = {}
    # The sets drop the duplicates a title in two sections' rows would give; a DISTINCT in
    # SQL builds a temp b-tree over every row first, several times the cost of the scan.
    for section_id, item_id in store_conn.execute(
        "SELECT p.section_id, p.item_id FROM plex_items p "
        "JOIN items i ON i.item_id = p.item_id WHERE i.type IN ('movie', 'episode')"
    ):
        sections.setdefault(section_id, set()).add(item_id)
    return sections


def _section_order(section_id: str) -> tuple[int, int, str]:
    return (0, int(section_id), "") if section_id.isdigit() else (1, 0, section_id)


def status(
    frames_conn: sqlite3.Connection,
    sections: dict[str, set[str]],
    *,
    path: Path,
    hours: str,
    now: datetime,
    encoder_reachable: bool | None,
    ffmpeg_found: bool,
    decoder: str = "cpu",
    failed_limit: int | None = None,
) -> dict[str, Any]:
    """What `plexdb frames status` prints. Its shape is documented in
    docs/schema.md; a key added here goes there too. `encoder_reachable` is
    `None` when no encoder is configured.

    `sections` is the result of `section_titles`, which scans every title in
    the store: a caller that asks often keeps it between calls. `failed_limit`
    keeps only the newest N failures in `failed`; `failed_total` counts them all."""
    indexed = {
        str(row[0])
        for row in frames_conn.execute(
            "SELECT item_id FROM frame_index_state WHERE error IS NULL"
        ).fetchall()
    }
    failed_total = int(
        frames_conn.execute(
            "SELECT count(*) FROM frame_index_state WHERE error IS NOT NULL"
        ).fetchone()[0]
    )
    failed = frames_conn.execute(
        "SELECT item_id, error, reason, indexed_at FROM frame_index_state "
        "WHERE error IS NOT NULL ORDER BY indexed_at DESC, item_id LIMIT ?",
        (-1 if failed_limit is None else failed_limit,),
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
        "indexing": _indexing(frames_conn) if harvester_running(path) else None,
        "encoder_reachable": encoder_reachable,
        "ffmpeg_found": ffmpeg_found,
        "decoder": decoder,
        "failed_total": failed_total,
        "failed": [
            {
                "item_id": row["item_id"],
                "error": row["error"],
                "reason": row["reason"],
                "at": row["indexed_at"],
            }
            for row in failed
        ],
    }


def _indexing(frames_conn: sqlite3.Connection) -> dict[str, str] | None:
    row = frames_conn.execute("SELECT item_id, since FROM frame_harvest").fetchone()
    return None if row is None else {"item_id": row["item_id"], "since": row["since"]}


def _ofd_lock(handle: IO[str], lock_type: int, command: int) -> bytes:
    """Run one open-file-description lock `command` over the whole of `handle`."""
    if _FLOCK_DARWIN:
        request = struct.pack(_FLOCK_FORMAT, 0, 0, 0, lock_type, os.SEEK_SET)
    else:
        request = struct.pack(_FLOCK_FORMAT, lock_type, os.SEEK_SET, 0, 0, 0)
    return fcntl.fcntl(handle, command, request)


def harvester_running(path: Path) -> bool:
    """Whether a harvester holds `harvest.lock` now. Asks the operating system which lock
    would block a write lock (`F_OFD_GETLK`) and takes none, so the probe never makes a
    starting harvester see a holder; a missing lock file means no harvester has ever run."""
    try:
        handle = open(path.parent / "harvest.lock")
    except FileNotFoundError:
        return False
    with handle:
        answer = _ofd_lock(handle, fcntl.F_WRLCK, fcntl.F_OFD_GETLK)
    lock_type = struct.unpack(_FLOCK_FORMAT, answer)[3 if _FLOCK_DARWIN else 0]
    return bool(lock_type != fcntl.F_UNLCK)


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
