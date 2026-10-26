"""A copy of the store is one self-contained file, never overwrites an older
copy, and goes back without its replaced file's sidecars. `plexdb migrate`'s own
copy and rollback are tested in Rust (`crates/plexdb/tests/migrate.rs`)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fixture_store import init

from plexdb import backup, store
from plexdb.errors import StoreError


def _store_with_history(path: Path, plays: int = 3) -> None:
    """A store at the current schema holding `plays` rows of history."""
    init(path)
    with store.open_store(path) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'A')")
        for n in range(plays):
            conn.execute(
                "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) "
                "VALUES (?, ?, ?, ?)",
                (f"h{n}", "imdb:tt1", 1, f"2026-01-0{n + 1}T00:00:00Z"),
            )
        conn.commit()


def test_a_backup_is_one_file_with_no_wal_sidecars(tmp_path: Path) -> None:
    path = tmp_path / "plexdb.db"
    _store_with_history(path)

    taken = backup.take(path, tmp_path / "backups", "plexdb.pre-v9")

    assert taken.path.exists()
    assert not Path(f"{taken.path}-wal").exists()
    assert not Path(f"{taken.path}-shm").exists()


def test_a_backup_carries_the_rows_the_store_had(tmp_path: Path) -> None:
    path = tmp_path / "plexdb.db"
    _store_with_history(path, plays=3)

    taken = backup.take(path, tmp_path / "backups", "plexdb.pre-v9")

    assert taken.counts["plays"] == 3
    conn = sqlite3.connect(taken.path)
    try:
        assert conn.execute("SELECT count(*) FROM plays").fetchone()[0] == 3
    finally:
        conn.close()


def test_a_second_backup_at_the_same_version_does_not_overwrite_the_first(
    tmp_path: Path,
) -> None:
    """A migration can be attempted, rolled back, and attempted again — all
    wanting the name `pre-v9`. The copy taken before the first attempt is the
    older and more valuable one, so it must survive the second."""
    path = tmp_path / "plexdb.db"
    _store_with_history(path)
    backups = tmp_path / "backups"

    first = backup.take(path, backups, "plexdb.pre-v9")
    second = backup.take(path, backups, "plexdb.pre-v9")

    assert first.path != second.path
    assert first.path.exists()
    assert sorted(p.name for p in backups.iterdir()) == [
        "plexdb.pre-v9.2.db",
        "plexdb.pre-v9.db",
    ]


def test_backing_up_a_store_that_does_not_exist_says_so(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="does not exist"):
        backup.take(tmp_path / "absent.db", tmp_path / "backups", "plexdb.pre-v9")


def test_restore_removes_the_wal_sidecar_of_the_file_it_replaced(tmp_path: Path) -> None:
    """A stale `-wal` left beside a restored database describes the file that
    was replaced, and SQLite reading it against different content is how a
    restore becomes a corruption."""
    path = tmp_path / "plexdb.db"
    _store_with_history(path)
    taken = backup.take(path, tmp_path / "backups", "plexdb.pre-v9")
    Path(f"{path}-wal").write_bytes(b"stale")

    backup.restore(taken.path, path)

    assert not Path(f"{path}-wal").exists()


def test_a_table_the_migration_creates_is_not_reported_as_row_loss(tmp_path: Path) -> None:
    """`guarded_counts` leaves out a table that does not exist yet. Counting it
    as zero would make the step that creates it look like it destroyed
    something."""
    path = tmp_path / "plexdb.db"
    init(path)
    with store.open_store(path) as conn:
        conn.execute("DROP TABLE plays")
        conn.commit()
        counts = backup.guarded_counts(conn)

    assert "plays" not in counts
    assert "items" in counts
