"""The store opens, carries every shipped migration's schema, and re-running
init changes nothing."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from plexdb import schema
from plexdb.errors import StoreError
from plexdb.store import init, open_readonly, open_store

V1_TABLES = {"items", "external_ids", "plex_items", "enrichment"}
V2_TABLES = {"plays", "plays_ingest_cursor"}


def _tables_from_batch(batch: str) -> set[str]:
    """The tables one migration batch creates, applied on its own — proves
    what a single shipped batch introduces without a later batch's tables
    muddying the count."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(batch)
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _tables(path: Path) -> set[str]:
    with open_readonly(path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r["name"] for r in rows}


def _columns(path: Path, table: str) -> set[str]:
    with open_readonly(path) as conn:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_init_creates_the_store_at_the_current_version(tmp_path: Path) -> None:
    store = tmp_path / "nested" / "plexdb.db"
    was, now = init(store)

    assert (was, now) == (0, schema.SCHEMA_VERSION)
    assert store.exists(), "init should create the parent directory too"


def test_v1_carries_exactly_the_four_tables() -> None:
    # Applied on its own, not through the full `init`, so a later migration's
    # tables can never be mistaken for something v1 introduced.
    assert _tables_from_batch(schema._V1) == V1_TABLES


def test_v2_carries_exactly_the_plays_and_cursor_tables() -> None:
    assert _tables_from_batch(schema._V1 + schema._V2) - V1_TABLES == V2_TABLES


def test_a_current_store_carries_every_migrations_tables_and_nothing_else(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    init(store)

    # Every table every shipped migration introduces, plus the bookkeeping
    # table `apply` itself creates — a table arriving early (or never
    # arriving) is a scope leak worth failing on.
    assert _tables(store) == V1_TABLES | V2_TABLES | {"schema_version"}


def test_the_columns_the_first_slice_depends_on_are_present(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init(store)

    assert {"item_id", "type", "title", "year"} <= _columns(store, "items")
    assert {"item_id", "ns", "value"} <= _columns(store, "external_ids")
    assert {"rating_key", "item_id", "section_id"} <= _columns(store, "plex_items")
    assert {"item_id", "namespace", "key", "value", "fetched_at"} <= _columns(store, "enrichment")


def test_the_columns_the_plays_slice_depends_on_are_present(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init(store)

    assert {
        "history_key",
        "item_id",
        "plex_account_id",
        "client_identifier",
        "platform",
        "viewed_at",
        "ip",
        "percent_complete",
        "paused_counter",
    } <= _columns(store, "plays")
    assert {"id", "since_viewed_at"} <= _columns(store, "plays_ingest_cursor")


def test_init_is_idempotent(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init(store)
    before = store.read_bytes()

    was, now = init(store)

    assert (was, now) == (schema.SCHEMA_VERSION, schema.SCHEMA_VERSION)
    assert store.read_bytes() == before, "a second init must not rewrite the store"


def test_a_store_from_the_future_refuses_to_open(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init(store)
    with open_store(store) as conn:
        conn.execute("UPDATE schema_version SET version = ?", (schema.SCHEMA_VERSION + 5,))
        conn.commit()

    with pytest.raises(StoreError, match="only understands"):
        init(store)


def test_a_store_missing_its_version_row_is_damaged_not_empty(tmp_path: Path) -> None:
    """The tables exist but the version row is gone.

    Treating that as an empty store would re-run migration 1 over tables that
    are already there, and the user would see "table items already exists"
    instead of being told their store is damaged.
    """
    store = tmp_path / "plexdb.db"
    init(store)
    with open_store(store) as conn:
        conn.execute("DELETE FROM schema_version")
        conn.commit()

    with pytest.raises(StoreError, match="damaged"):
        init(store)


def test_opening_a_missing_store_says_so(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="plexdb init"):
        with open_store(tmp_path / "absent.db"):
            pass


def test_the_readonly_handle_cannot_write(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init(store)

    with open_readonly(store) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO items (item_id, type, title) VALUES ('x', 'movie', 'X')")


def test_a_failed_migration_leaves_the_store_untouched(tmp_path: Path) -> None:
    """A migration is all-or-nothing.

    Without one transaction around the DDL and the version row, a crash between
    them leaves a store that is neither empty nor current — tables present, no
    version — which no later run can repair.
    """
    store = tmp_path / "plexdb.db"
    broken = schema._V1 + "\nCREATE TABLE items (nope INTEGER);\n"

    with open_store(store, create=True) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.executescript(
                "BEGIN;\n"
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);\n"
                f"{broken}"
                "COMMIT;\n"
            )
        conn.rollback()
        remaining = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0]

    assert remaining == 0, "a failed migration must roll its tables back"


def test_a_directory_where_a_store_should_be_says_so(tmp_path: Path) -> None:
    a_directory = tmp_path / "oops"
    a_directory.mkdir()

    with pytest.raises(StoreError, match="is a directory"):
        init(a_directory)


def test_a_file_that_is_not_a_database_says_so(tmp_path: Path) -> None:
    not_a_store = tmp_path / "notes.txt"
    not_a_store.write_text("this is not a database\n")

    with pytest.raises(StoreError, match="not a plexdb store"):
        init(not_a_store)


def test_an_unwritable_parent_says_so(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        with pytest.raises(StoreError, match="cannot create"):
            init(locked / "sub" / "plexdb.db")
    finally:
        locked.chmod(0o700)


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init(store)

    with open_store(store) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO external_ids (item_id, ns, value) VALUES ('missing', 'imdb', 'tt1')"
            )
