"""Publishing a read-only snapshot for consumers (ADR-0007, issue #15).

The acceptance criterion that matters: the snapshot opens read-only from a
directory with no write permission — the one arrangement that fails for the
live WAL store, because WAL needs to write its `-wal`/`-shm` sidecars even for
a read-only connection.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from plexdb import schema
from plexdb.errors import StoreError
from plexdb.store import init, open_readonly, open_store, publish


def _seed(store: Path) -> None:
    init(store)
    with open_store(store) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('tmdb:1', 'movie', 'One')")
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('tmdb:2', 'movie', 'Two')")
        conn.commit()


def test_publish_produces_one_file_with_no_wal_sidecars(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    snapshot = tmp_path / "out" / "plexdb.snapshot.db"
    _seed(store)

    publish(store, snapshot)

    assert snapshot.exists()
    assert not snapshot.with_name(snapshot.name + "-wal").exists()
    assert not snapshot.with_name(snapshot.name + "-shm").exists()
    assert not snapshot.with_name(snapshot.name + ".tmp").exists()


def test_the_snapshot_opens_readonly_from_a_directory_with_no_write_permission(
    tmp_path: Path,
) -> None:
    """The whole point of the issue: a mode-555 directory is where the live
    WAL store fails to open read-only, and the published snapshot must not."""
    store = tmp_path / "plexdb.db"
    locked_dir = tmp_path / "locked"
    locked_dir.mkdir()
    snapshot = locked_dir / "plexdb.snapshot.db"
    _seed(store)

    publish(store, snapshot)
    locked_dir.chmod(0o500)
    try:
        with open_readonly(snapshot) as conn:
            rows = conn.execute("SELECT item_id FROM items ORDER BY item_id").fetchall()
        assert [r["item_id"] for r in rows] == ["tmdb:1", "tmdb:2"]
    finally:
        locked_dir.chmod(0o700)


def test_a_write_through_the_snapshots_readonly_handle_raises(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    snapshot = tmp_path / "plexdb.snapshot.db"
    _seed(store)
    publish(store, snapshot)

    with open_readonly(snapshot) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO items (item_id, type, title) VALUES ('x', 'movie', 'X')")


def test_the_snapshot_carries_the_same_version_and_row_counts_as_the_live_store(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    snapshot = tmp_path / "plexdb.snapshot.db"
    _seed(store)

    version, _size = publish(store, snapshot)

    assert version == schema.SCHEMA_VERSION
    with open_readonly(snapshot) as conn:
        snapshot_version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        snapshot_count = conn.execute("SELECT count(*) FROM items").fetchone()[0]
    with open_readonly(store) as conn:
        live_count = conn.execute("SELECT count(*) FROM items").fetchone()[0]

    assert snapshot_version == schema.SCHEMA_VERSION
    assert snapshot_count == live_count == 2


def test_publishing_twice_succeeds_and_leaves_one_snapshot(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    out_dir = tmp_path / "out"
    snapshot = out_dir / "plexdb.snapshot.db"
    _seed(store)

    publish(store, snapshot)
    with open_store(store) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('tmdb:3', 'movie', 'Three')")
        conn.commit()
    publish(store, snapshot)

    assert sorted(p.name for p in out_dir.iterdir()) == [snapshot.name]
    with open_readonly(snapshot) as conn:
        count = conn.execute("SELECT count(*) FROM items").fetchone()[0]
    assert count == 3, "a second publish should replace the snapshot, not append to it"


def test_publish_replaces_an_existing_snapshot_atomically_not_partially(tmp_path: Path) -> None:
    """A reader must never see a half-written file.

    Simulated by holding a read handle open on the first snapshot across the
    second publish: an atomic rename lets that handle keep reading the old
    (complete) file rather than a truncated one.
    """
    store = tmp_path / "plexdb.db"
    snapshot = tmp_path / "plexdb.snapshot.db"
    _seed(store)
    publish(store, snapshot)

    with open_readonly(snapshot) as held:
        with open_store(store) as conn:
            conn.execute(
                "INSERT INTO items (item_id, type, title) VALUES ('tmdb:3', 'movie', 'Three')"
            )
            conn.commit()
        publish(store, snapshot)

        # The handle opened before the second publish still sees a complete,
        # consistent file — the original two rows, never a truncated read.
        held_count = held.execute("SELECT count(*) FROM items").fetchone()[0]
        assert held_count == 2

    with open_readonly(snapshot) as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 3


def test_publishing_from_a_missing_store_says_so(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="plexdb migrate"):
        publish(tmp_path / "absent.db", tmp_path / "out.db")


def test_publish_reports_schema_version_and_byte_size(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    snapshot = tmp_path / "plexdb.snapshot.db"
    _seed(store)

    version, size = publish(store, snapshot)

    assert version == schema.SCHEMA_VERSION
    assert size == snapshot.stat().st_size
    assert size > 0


def test_a_bad_snapshot_parent_says_so_not_a_traceback(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    _seed(store)
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        with pytest.raises(StoreError, match="cannot create"):
            publish(store, locked / "sub" / "plexdb.snapshot.db")
    finally:
        locked.chmod(0o700)
