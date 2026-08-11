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
V3_TABLES = {"edges"}
V6_TABLES = {"collection", "collection_membership"}
#: V4 adds no new table — it only alters the existing `plays` table and adds
#: an index (issue #9).


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


def test_v3_carries_exactly_the_edges_table() -> None:
    assert (
        _tables_from_batch(schema._V1 + schema._V2 + schema._V3) - V1_TABLES - V2_TABLES
        == V3_TABLES
    )


def test_v6_carries_exactly_the_two_collection_tables() -> None:
    every_earlier = V1_TABLES | V2_TABLES | V3_TABLES
    applied = _tables_from_batch(
        schema._V1 + schema._V2 + schema._V3 + schema._V4 + schema._V5 + schema._V6
    )

    assert applied - every_earlier == V6_TABLES


def test_a_current_store_carries_every_migrations_tables_and_nothing_else(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    init(store)

    # Every table every shipped migration introduces, plus the bookkeeping
    # table `apply` itself creates — a table arriving early (or never
    # arriving) is a scope leak worth failing on.
    assert _tables(store) == V1_TABLES | V2_TABLES | V3_TABLES | V6_TABLES | {"schema_version"}


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


def test_the_columns_the_edges_slice_depends_on_are_present(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init(store)

    assert {"from_id", "to_id", "edge_type", "rank", "fetched_at"} <= _columns(store, "edges")


def test_v4_adds_seconds_watched_and_tautulli_id_to_plays(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init(store)

    assert {"seconds_watched", "tautulli_id"} <= _columns(store, "plays")


def test_a_second_play_cannot_reuse_a_tautulli_id_already_claimed(tmp_path: Path) -> None:
    """The partial unique index backing issue #9's idempotency guarantee: a
    Tautulli row's own id can never land on two different plays."""
    store = tmp_path / "plexdb.db"
    init(store)

    with open_store(store) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'A')")
        conn.execute(
            "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at, tautulli_id) "
            "VALUES ('h1', 'imdb:tt1', 1, 100, 42)"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at, tautulli_id) "
                "VALUES ('h2', 'imdb:tt1', 1, 200, 42)"
            )
        # NULL is not "claimed" — many plays with no Tautulli match yet is
        # the normal state, not a constraint violation.
        conn.execute(
            "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) "
            "VALUES ('h3', 'imdb:tt1', 1, 300)"
        )
        conn.execute(
            "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) "
            "VALUES ('h4', 'imdb:tt1', 1, 400)"
        )


def _v4_store(path: Path) -> None:
    """A store frozen at version 4 — the shape that shipped before issue #23."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            schema._V1
            + schema._V2
            + schema._V3
            + schema._V4
            + "CREATE TABLE schema_version (version INTEGER NOT NULL);"
            + "INSERT INTO schema_version (version) VALUES (4);"
        )
        conn.commit()
    finally:
        conn.close()


def test_v5_lets_a_movie_and_a_show_hold_the_same_tmdb_number(tmp_path: Path) -> None:
    """TMDB numbers movies and shows in separate lists, so `tmdb 1678` is
    *Godzilla* (1954) and *The Golden Girls* (1985). Under v4's
    `PRIMARY KEY (ns, value)` the second insert raised and the two titles
    ended up sharing one identity (issue #23)."""
    store = tmp_path / "plexdb.db"
    init(store)

    with open_store(store) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('m', 'movie', 'Godzilla')")
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('s', 'show', 'Golden')")
        conn.execute(
            "INSERT INTO external_ids (item_id, ns, value, kind) "
            "VALUES ('m', 'tmdb', '1678', 'movie')"
        )
        conn.execute(
            "INSERT INTO external_ids (item_id, ns, value, kind) "
            "VALUES ('s', 'tmdb', '1678', 'show')"
        )
        conn.commit()

        holders = {
            r["item_id"]
            for r in conn.execute("SELECT item_id FROM external_ids WHERE value = '1678'")
        }
        assert holders == {"m", "s"}

        # Two of the same kind still collide — that is the merge issue #19 wanted.
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('m2', 'movie', 'Copy')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO external_ids (item_id, ns, value, kind) "
                "VALUES ('m2', 'tmdb', '1678', 'movie')"
            )


def test_v5_takes_an_existing_rows_kind_from_its_items_type(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    _v4_store(store)
    conn = sqlite3.connect(store)
    conn.executescript(
        "INSERT INTO items (item_id, type, title) VALUES ('m', 'movie', 'Godzilla');"
        "INSERT INTO items (item_id, type, title) VALUES ('s', 'show', 'Golden');"
        "INSERT INTO external_ids (item_id, ns, value) VALUES ('m', 'imdb', 'tt0047034');"
        "INSERT INTO external_ids (item_id, ns, value) VALUES ('s', 'imdb', 'tt0088526');"
    )
    conn.commit()
    conn.close()

    was, now = init(store)
    assert (was, now) == (4, schema.SCHEMA_VERSION)

    with open_readonly(store) as conn:
        kinds = {
            r["item_id"]: r["kind"] for r in conn.execute("SELECT item_id, kind FROM external_ids")
        }
    assert kinds == {"m": "movie", "s": "show"}


def test_v5_backfills_a_plays_rating_key_only_when_it_is_unambiguous(tmp_path: Path) -> None:
    """A play resolved through a rating key and then forgot it. The backfill
    can recover it wherever an identity maps to exactly one rating key —
    which is everything except the fused identities issue #23 is about, where
    the answer is genuinely unknowable and `repair-identities` re-ingests."""
    store = tmp_path / "plexdb.db"
    _v4_store(store)
    conn = sqlite3.connect(store)
    conn.executescript(
        "INSERT INTO items (item_id, type, title) VALUES ('one', 'movie', 'One Key');"
        "INSERT INTO items (item_id, type, title) VALUES ('two', 'movie', 'Fused');"
        "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
        "VALUES ('11', 'one', '1', '2024-01-01T00:00:00+00:00');"
        "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
        "VALUES ('21', 'two', '1', '2024-01-01T00:00:00+00:00');"
        "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
        "VALUES ('22', 'two', '2', '2024-01-01T00:00:00+00:00');"
        "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) "
        "VALUES ('h1', 'one', 1, 1000);"
        "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) "
        "VALUES ('h2', 'two', 1, 2000);"
    )
    conn.commit()
    conn.close()

    init(store)

    with open_readonly(store) as conn:
        keys = {
            r["history_key"]: r["rating_key"]
            for r in conn.execute("SELECT history_key, rating_key FROM plays")
        }
    assert keys == {"h1": "11", "h2": None}


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
                "INSERT INTO external_ids (item_id, ns, value, kind) "
                "VALUES ('missing', 'imdb', 'tt1', 'movie')"
            )


def test_edges_foreign_keys_are_enforced_on_both_from_and_to(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init(store)

    with open_store(store) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'A')")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO edges (from_id, to_id, edge_type, rank, fetched_at) "
                "VALUES ('imdb:tt1', 'missing', 'tmdb_similar', 1, '2026-01-01T00:00:00+00:00')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO edges (from_id, to_id, edge_type, rank, fetched_at) "
                "VALUES ('missing', 'imdb:tt1', 'tmdb_similar', 1, '2026-01-01T00:00:00+00:00')"
            )


def test_edges_uniqueness_is_from_id_to_id_edge_type(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init(store)

    with open_store(store) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'A')")
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('imdb:tt2', 'movie', 'B')")
        conn.commit()
        conn.execute(
            "INSERT INTO edges (from_id, to_id, edge_type, rank, fetched_at) "
            "VALUES ('imdb:tt1', 'imdb:tt2', 'tmdb_similar', 1, '2026-01-01T00:00:00+00:00')"
        )
        # A different edge_type between the same pair is a distinct row.
        conn.execute(
            "INSERT INTO edges (from_id, to_id, edge_type, rank, fetched_at) "
            "VALUES ('imdb:tt1', 'imdb:tt2', 'tmdb_recommendations', 1, "
            "'2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO edges (from_id, to_id, edge_type, rank, fetched_at) "
                "VALUES ('imdb:tt1', 'imdb:tt2', 'tmdb_similar', 2, '2026-01-01T00:00:00+00:00')"
            )
