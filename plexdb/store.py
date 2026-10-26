"""Opening the store.

One process writes this file and everything else reads it, so the read-only
path is a real mode here rather than a convention — a reader that *can* write is
a bug waiting for a caller.
"""

from __future__ import annotations

import fcntl
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
        raise FileNotFoundError(f"no store at {path} — run `plexdb migrate` first")

    with writing(path):
        conn = _connect(path)
        try:
            yield conn
        finally:
            conn.close()


def _write_lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".write-lock")


@contextmanager
def writing(path: Path) -> Iterator[None]:
    """Hold the writer lock shared: around every writing connection, and around
    a whole sweep so the gaps between its steps count as writing too.

    Shared, so writers never wait on each other here — SQLite serialises their
    transactions. The lock exists so `held_by` can see a writer from outside the
    process: the nightly sweep runs inside `plexdb schedule`, where no process
    name gives it away.
    """
    if _is_held(path):
        yield
        return
    _ensure_parent(path)
    with _write_lock_path(path).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        yield


#: Stores this process holds through `holding`. `writing` takes its own file
#: handle, and a second handle on a file this process already holds exclusive
#: would wait on the first forever, so it stands down for a store listed here.
_held: set[Path] = set()


def _is_held(path: Path) -> bool:
    return path.resolve() in _held


def held_path(path: Path) -> Path | None:
    """The resolved path of `path` when this process holds it through `holding`,
    else `None`. A child process that writes the store is told it (`rust.run`), so
    it stands down rather than wait on this process's exclusive lock."""
    resolved = path.resolve()
    return resolved if resolved in _held else None


@contextmanager
def holding(path: Path) -> Iterator[str | None]:
    """Claim the whole store for this process, or say what stops it.

    Takes the migration lock and the write lock both exclusive without
    blocking, and yields `None` once it holds them, or a reason (the same words
    as `held_by`) and holds nothing. Checking and claiming are one act, so a
    sweep or migration cannot start between "nothing is running" and the
    caller's command; one that wakes meanwhile waits for the claim to end.
    Inside the block `writing`, `outside_migration` and a nested `holding` do
    not lock again, and a `plexdb-rs` child is told to stand down (`rust.run`).
    `held_by` still reports the claim, because it answers what another process
    would see.
    """
    if _is_held(path):
        yield None
        return
    _ensure_parent(path)
    with _lock_path(path).open("a") as migrate_handle:
        try:
            fcntl.flock(migrate_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield "a migration is running"
            return
        with _write_lock_path(path).open("a") as write_handle:
            try:
                fcntl.flock(write_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield "a writer has the store open"
                return
            _held.add(path.resolve())
            try:
                yield None
            finally:
                _held.discard(path.resolve())


def held_by(path: Path) -> str | None:
    """Say what holds the store right now, or `None` when nothing does.

    Creates nothing: a store no writer or migration has locked yet has no lock
    files, and reads as idle.
    """
    with outside_migration(path) as clear:
        if not clear:
            return "a migration is running"
    try:
        handle = _write_lock_path(path).open("r")
    except FileNotFoundError:
        return None
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "a writer has the store open"
    return None


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
def read_snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """Make every read inside the block see the store as of its first one.

    Outside a transaction each SELECT is its own read, so a commit landing
    between two of them pairs rows from before it with rows from after. A
    savepoint rather than `BEGIN`: on a connection already inside a
    transaction — a writer with pending rows — it nests, and its release
    leaves that transaction and its rows as they were.
    """
    conn.execute("SAVEPOINT read_snapshot")
    try:
        yield
    finally:
        conn.execute("RELEASE read_snapshot")


@contextmanager
def open_readonly(path: Path) -> Iterator[sqlite3.Connection]:
    """Open the store read-only. Any write raises rather than being applied."""
    if path.is_dir():
        # Without this, SQLite opens a directory happily and the first statement
        # fails with `disk I/O error`, which reads as a failing disk rather than
        # a path pointing one level too high. `open_store` has said so plainly
        # since it was written; a reader deserves the same sentence.
        raise StoreError(f"{path} is a directory, not a store — point PLEXDB_PATH at a file")
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


def quick_check(conn: sqlite3.Connection) -> str:
    """SQLite's own verdict on whether the file is structurally sound.

    `"ok"` means sound; anything else is the first problem it found. Read-only,
    so `plexdb check` runs on a live store the same pragma `plexdb migrate` runs
    on a just-migrated one — there is one definition of "damaged", not two.
    """
    return str(conn.execute("PRAGMA quick_check").fetchone()[0])


def _lock_path(path: Path) -> Path:
    """The file `plexdb migrate` holds exclusive for the whole of a migration,
    rollback included (`plexdb_store::migrate_lock_path`)."""
    return path.with_name(path.name + ".migrate-lock")


@contextmanager
def outside_migration(path: Path) -> Iterator[bool]:
    """Yield whether no migration is running, and keep one from starting while
    the caller reads.

    Takes the migration lock shared without blocking, and creates nothing: a
    store no migration has locked yet has no lock file, and reads as clear. A
    store this process holds through `holding` reads as clear too: the exclusive
    lock is its own, not a migration's.
    """
    if _is_held(path):
        yield True
        return
    try:
        handle = _lock_path(path).open("r")
    except FileNotFoundError:
        yield True
        return
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


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
