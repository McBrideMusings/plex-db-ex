"""A migration takes a copy first, keeps it, and puts it back when the result
is wrong."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from plexdb import backup, schema, store
from plexdb.errors import StoreError


def _store_with_history(path: Path, plays: int = 3) -> None:
    """A store at the current schema holding `plays` rows of history."""
    store.init(path)
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


def test_migrating_an_already_current_store_takes_no_backup(tmp_path: Path) -> None:
    """Every sweep runs init. A copy per sweep would fill the disk with
    identical files."""
    path = tmp_path / "plexdb.db"
    _store_with_history(path)
    backups = tmp_path / "backups"

    result = store.migrate(path, backups)

    assert not result.migrated
    assert result.backup is None
    assert not backups.exists()


def test_creating_a_store_from_nothing_takes_no_backup(tmp_path: Path) -> None:
    path = tmp_path / "plexdb.db"

    result = store.migrate(path, tmp_path / "backups")

    assert result.was == 0
    assert result.now == schema.SCHEMA_VERSION
    assert result.backup is None


def _store_one_version_behind(path: Path, plays: int = 3) -> None:
    """A real store at `SCHEMA_VERSION - 1`, with history in it.

    Built by applying every migration but the last, rather than by creating a
    current store and rewriting its version row — that would leave the last
    migration's tables already present, so re-applying it fails for a reason
    that has nothing to do with what the test is checking.
    """
    behind = schema.SCHEMA_VERSION - 1
    conn = sqlite3.connect(path)
    try:
        for migration in schema.MIGRATIONS[:behind]:
            if isinstance(migration, str):
                conn.executescript(migration)
            else:
                migration(conn)
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (behind,))
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'A')")
        for n in range(plays):
            conn.execute(
                "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) "
                "VALUES (?, ?, ?, ?)",
                (f"h{n}", "imdb:tt1", 1, f"2026-01-0{n + 1}T00:00:00Z"),
            )
        conn.commit()
    finally:
        conn.close()


def test_a_real_migration_takes_a_backup_and_keeps_it(tmp_path: Path) -> None:
    """A store one version behind is copied before the migration runs, and the
    copy still holds the pre-migration rows afterwards."""
    path = tmp_path / "plexdb.db"
    _store_one_version_behind(path, plays=2)

    result = store.migrate(path, tmp_path / "backups")

    assert result.was == schema.SCHEMA_VERSION - 1
    assert result.now == schema.SCHEMA_VERSION
    assert result.backup is not None
    assert result.backup.exists()
    assert result.counts_before["plays"] == 2


def test_a_migration_that_loses_history_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transaction in `schema.apply` catches a crash. It cannot catch SQL
    that runs perfectly and deletes the wrong thing — that is what the row
    guard is for, and it must put the rows back."""
    path = tmp_path / "plexdb.db"
    _store_one_version_behind(path, plays=3)

    monkeypatch.setattr(
        schema,
        "MIGRATIONS",
        schema.MIGRATIONS[: schema.SCHEMA_VERSION - 1] + ("DELETE FROM plays;\n",),
    )

    with pytest.raises(StoreError, match="a migration lost 3 rows"):
        store.migrate(path, tmp_path / "backups")

    with store.open_readonly(path) as conn:
        assert conn.execute("SELECT count(*) FROM plays").fetchone()[0] == 3
        assert schema.current_version(conn) == schema.SCHEMA_VERSION - 1


def test_a_migration_that_raises_is_rolled_back_and_names_the_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "plexdb.db"
    _store_one_version_behind(path, plays=1)

    monkeypatch.setattr(
        schema,
        "MIGRATIONS",
        schema.MIGRATIONS[: schema.SCHEMA_VERSION - 1] + ("SELECT this_is_not_valid_sql(;\n",),
    )

    with pytest.raises(StoreError, match="rolled back from"):
        store.migrate(path, tmp_path / "backups")

    with store.open_readonly(path) as conn:
        assert conn.execute("SELECT count(*) FROM plays").fetchone()[0] == 1


def _v9_store_with_colliding_keywords(path: Path) -> None:
    """A real store at v9, holding two keyword spellings on one item
    (`Heists` and `heist`) that will collide once v10 stems and merges them —
    the shape that made `store.migrate`'s row-count guard misread an
    intended merge as data loss before this test existed."""
    conn = sqlite3.connect(path)
    try:
        script = "BEGIN;\nCREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);\n"
        script += "".join(m for m in schema.MIGRATIONS[:9] if isinstance(m, str))
        script += "INSERT INTO schema_version (version) VALUES (9);\nCOMMIT;\n"
        conn.executescript(script)
        conn.execute(
            "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'Heat')"
        )
        conn.executemany(
            "INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) "
            "VALUES ('imdb:tt1', 'tmdb_keywords', 'keyword', ?, ?)",
            [
                ("Heists", "2026-01-01T00:00:00+00:00"),
                ("heist", "2026-01-02T00:00:00+00:00"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_migrate_lands_a_colliding_pre_v10_store_at_v10_with_rows_merged(
    tmp_path: Path,
) -> None:
    """Acceptance: `store.migrate` — the real `plexdb migrate` path, which
    `_verify` guards — must not roll back v10 just because two colliding
    keyword spellings merged into one `enrichment` row. Every test that
    exercised `_V10` before this one ran it through `schema.apply`/`init`
    directly, never through `store.migrate`, so this guard was never proven
    against the exact migration it exists to police."""
    path = tmp_path / "plexdb.db"
    _v9_store_with_colliding_keywords(path)

    result = store.migrate(path, tmp_path / "backups")

    assert result.was == 9
    assert result.now == schema.SCHEMA_VERSION
    assert result.backup is not None

    with store.open_readonly(path) as conn:
        rows = conn.execute(
            "SELECT namespace, source, key, value FROM enrichment WHERE item_id = 'imdb:tt1'"
        ).fetchall()
    assert [tuple(r) for r in rows] == [("keywords", "tmdb", "keyword", "heist")]

    # Idempotent: already current, nothing changes on a second run.
    second = store.migrate(path, tmp_path / "backups")
    assert not second.migrated


def test_migrate_still_refuses_a_v10_that_also_loses_an_unrelated_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared-shrink guard (`store._verify`) accepts v10's merge only
    down to the exact row count `_V10` itself computed. A `_V10` that merges
    as expected *and* drops something else — simulating a bug in the
    migration itself — must still be rolled back, not waved through just
    because *some* shrink was declared."""
    path = tmp_path / "plexdb.db"
    _v9_store_with_colliding_keywords(path)

    real_v10 = schema._V10

    def _buggy_v10(conn: sqlite3.Connection) -> schema.DeclaredShrinks:
        conn.execute(
            "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt2', 'movie', 'Ronin')"
        )
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) "
            "VALUES ('imdb:tt2', 'mdblist', 'list', 'top250', '2024-01-01T00:00:00+00:00')"
        )
        declared = real_v10(conn)
        # The bug: silently drops a fact this migration never touched.
        conn.execute("DELETE FROM enrichment WHERE item_id = 'imdb:tt2' AND namespace = 'mdblist'")
        return declared

    monkeypatch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:9] + (_buggy_v10,))
    monkeypatch.setattr(schema, "SCHEMA_VERSION", 10)

    with pytest.raises(StoreError, match="a migration lost 1 rows"):
        store.migrate(path, tmp_path / "backups")

    with store.open_readonly(path) as conn:
        assert schema.current_version(conn) == 9
        assert (
            conn.execute("SELECT count(*) FROM enrichment WHERE item_id = 'imdb:tt1'").fetchone()[0]
            == 2
        ), "rolled back to the pre-migration store, colliding rows intact"


def test_a_table_the_migration_creates_is_not_reported_as_row_loss(tmp_path: Path) -> None:
    """`guarded_counts` leaves out a table that does not exist yet. Counting it
    as zero would make the migration that creates it look like it destroyed
    something."""
    path = tmp_path / "plexdb.db"
    store.init(path)
    with store.open_store(path) as conn:
        conn.execute("DROP TABLE plays")
        conn.commit()
        counts = backup.guarded_counts(conn)

    assert "plays" not in counts
    assert "items" in counts
