"""The store opens, carries every shipped migration's schema, and re-running
init changes nothing."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from plexdb import health, schema
from plexdb.errors import StoreError
from plexdb.store import init, migrate, open_readonly, open_store

V1_TABLES = {"items", "external_ids", "plex_items", "enrichment"}
V2_TABLES = {"plays", "plays_ingest_cursor"}
V3_TABLES = {"edges"}
V6_TABLES = {"collection", "collection_membership"}
V7_TABLES = {"enrichment_cursor"}
V9_TABLES = {"title_map", "title_map_state"}
V10_TABLES = {"keyword_forms"}
V11_TABLES = {"tag_network", "tag_network_edge", "tag_network_state"}
V12_TABLES = {"keyword_pairs"}
V14_TABLES = {"keyword_roles", "keyword_role_decisions"}
V16_TABLES = {"keyword_role_statements"}
V17_TABLES = {"keyword_surfaces"}
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


def test_v7_moves_every_bookkeeping_row_out_of_enrichment(tmp_path: Path) -> None:
    """The migration that made `enrichment` a facts-only table (issue #41).

    Built by applying v1–v6, writing the sentinels exactly as the two TMDB
    writers used to, then running v7 alone — so this tests the migration
    against the shape it will actually meet on a real store, not against a
    store this version of the code created.
    """
    store = tmp_path / "plexdb.db"
    conn = sqlite3.connect(store)
    try:
        conn.executescript(
            schema._V1 + schema._V2 + schema._V3 + schema._V4 + schema._V5 + schema._V6
        )
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'X')")
        conn.executemany(
            "INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) "
            "VALUES ('imdb:tt1', ?, ?, ?, ?)",
            [
                ("tmdb_keywords", "keyword", "heist", "2026-01-01T00:00:00+00:00"),
                ("tmdb_keywords", "_fetched", "1", "2026-01-02T00:00:00+00:00"),
                ("tmdb_edges", "_fetched_recommendations", "1", "2026-01-03T00:00:00+00:00"),
                ("tmdb_edges", "_fetched_similar", "1", "2026-01-04T00:00:00+00:00"),
            ],
        )
        conn.commit()

        conn.executescript(schema._V7)

        facts = conn.execute(
            "SELECT namespace, key, value FROM enrichment ORDER BY namespace, key"
        ).fetchall()
        cursors = conn.execute(
            "SELECT namespace, key, fetched_at FROM enrichment_cursor ORDER BY namespace, key"
        ).fetchall()
    finally:
        conn.close()

    # Only the real keyword survives in `enrichment`.
    assert facts == [("tmdb_keywords", "keyword", "heist")]
    # All three cursors move, keep their own namespace, lose the `_` prefix,
    # and carry their original fetched_at — so no sweep re-fetches anything.
    assert cursors == [
        ("tmdb_edges", "fetched_recommendations", "2026-01-03T00:00:00+00:00"),
        ("tmdb_edges", "fetched_similar", "2026-01-04T00:00:00+00:00"),
        ("tmdb_keywords", "fetched", "2026-01-02T00:00:00+00:00"),
    ]


def test_v8_backfills_last_seen_on_every_existing_external_ids_row(tmp_path: Path) -> None:
    """Issue #57. Built against v1–v7's shape (mirroring the real store's
    starting point before this migration ever runs), with an existing
    external_ids row that predates the column entirely, then v8 applied
    alone."""
    store = tmp_path / "plexdb.db"
    conn = sqlite3.connect(store)
    try:
        conn.executescript(
            schema._V1 + schema._V2 + schema._V3 + schema._V4 + schema._V5 + schema._V6 + schema._V7
        )
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'X')")
        conn.execute(
            "INSERT INTO external_ids (item_id, ns, value, kind) "
            "VALUES ('imdb:tt1', 'imdb', 'tt1', 'movie')"
        )
        conn.commit()

        conn.executescript(schema._V8)

        rows = conn.execute("SELECT ns, value, last_seen FROM external_ids").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    ns, value, last_seen = rows[0]
    assert (ns, value) == ("imdb", "tt1")
    assert last_seen is not None


def test_a_current_store_carries_every_migrations_tables_and_nothing_else(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    init(store)

    # Every table every shipped migration introduces, plus the bookkeeping
    # table `apply` itself creates — a table arriving early (or never
    # arriving) is a scope leak worth failing on.
    assert _tables(store) == (
        V1_TABLES
        | V2_TABLES
        | V3_TABLES
        | V6_TABLES
        | V7_TABLES
        | V9_TABLES
        | V10_TABLES
        | V11_TABLES
        | V12_TABLES
        | V14_TABLES
        | V16_TABLES
        | V17_TABLES
        | {"reader_shape"}
        | {"schema_version"}
    )


def test_the_columns_the_first_slice_depends_on_are_present(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init(store)

    assert {"item_id", "type", "title", "year"} <= _columns(store, "items")
    assert {"item_id", "ns", "value", "last_seen"} <= _columns(store, "external_ids")
    assert {"rating_key", "item_id", "section_id"} <= _columns(store, "plex_items")
    assert {"item_id", "namespace", "source", "key", "value", "fetched_at"} <= _columns(
        store, "enrichment"
    )


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
            "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
            "VALUES ('m', 'tmdb', '1678', 'movie', '2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
            "VALUES ('s', 'tmdb', '1678', 'show', '2026-01-01T00:00:00+00:00')"
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
                "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
                "VALUES ('m2', 'tmdb', '1678', 'movie', '2026-01-01T00:00:00+00:00')"
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


def test_the_rust_reader_supports_exactly_the_current_reader_shape() -> None:
    """`plexdb-reader` refuses any store whose `reader_shape` is not its
    `SUPPORTED_READER_SHAPE`, so the two constants must agree."""
    reader_schema = (
        Path(__file__).parent.parent / "crates" / "plexdb-reader" / "src" / "schema.rs"
    ).read_text()
    match = re.search(r"^pub const SUPPORTED_READER_SHAPE: i64 = (\d+);$", reader_schema, re.M)
    assert match, "SUPPORTED_READER_SHAPE not found in crates/plexdb-reader/src/schema.rs"

    assert int(match.group(1)) == schema.READER_SHAPE


def test_a_migrated_store_carries_the_current_reader_shape(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init(store)
    with open_store(store) as conn:
        rows = [tuple(r) for r in conn.execute("SELECT version FROM reader_shape")]

    assert rows == [(schema.READER_SHAPE,)]


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
    with pytest.raises(FileNotFoundError, match="plexdb migrate"):
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
                "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
                "VALUES ('missing', 'imdb', 'tt1', 'movie', '2026-01-01T00:00:00+00:00')"
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


def test_v9_adds_the_stored_title_map_tables(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "plexdb.db")
    try:
        v1_through_v8 = "".join(m for m in schema.MIGRATIONS[:8] if isinstance(m, str))
        conn.executescript(v1_through_v8 + schema._V9)
        columns = {
            table: [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            for table in ("title_map", "title_map_state")
        }
    finally:
        conn.close()

    assert columns == {
        "title_map": ["kind", "item_id", "x", "y"],
        "title_map_state": ["kind", "fingerprint", "unplaced", "computed_at"],
    }


def _v9_store_with_keyword_data(path: Path) -> None:
    """A store at v9 (ADR-0016's starting shape) with pre-migration keyword
    rows: two spellings of the same idea on one item that will collide once
    stemmed, a third that will not, and one row under an unrelated namespace
    to exercise the "pick a fitting source" fallback."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            schema._V1
            + schema._V2
            + schema._V3
            + schema._V4
            + schema._V5
            + schema._V6
            + schema._V7
            + schema._V8
            + schema._V9
            + "CREATE TABLE schema_version (version INTEGER NOT NULL);"
            + "INSERT INTO schema_version (version) VALUES (9);"
        )
        conn.execute(
            "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'Heat')"
        )
        conn.execute(
            "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt2', 'movie', 'Ronin')"
        )
        conn.executemany(
            "INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                # Collide after stemming — the newer fetched_at should win.
                ("imdb:tt1", "tmdb_keywords", "keyword", "Heists", "2026-01-01T00:00:00+00:00"),
                ("imdb:tt1", "tmdb_keywords", "keyword", "heist", "2026-01-02T00:00:00+00:00"),
                # Distinct after stemming — survives as its own row.
                (
                    "imdb:tt1",
                    "tmdb_keywords",
                    "keyword",
                    "bank-heist",
                    "2026-01-03T00:00:00+00:00",
                ),
                # An unrelated namespace, untouched by the rename.
                ("imdb:tt2", "mdblist", "list", "top250", "2024-01-01T00:00:00+00:00"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_v10_renames_tmdb_keywords_backfills_source_and_stems_merging_collisions(
    tmp_path: Path,
) -> None:
    """ADR-0016. Built against the real v9 shape, then v10 run alone — the same
    pattern `test_v7_moves_every_bookkeeping_row_out_of_enrichment` uses."""
    store = tmp_path / "plexdb.db"
    _v9_store_with_keyword_data(store)
    conn = sqlite3.connect(store)
    try:
        schema._V10(conn)
        conn.commit()

        rows = conn.execute(
            "SELECT item_id, namespace, source, key, value, fetched_at FROM enrichment "
            "ORDER BY item_id, value"
        ).fetchall()
        forms = dict(conn.execute("SELECT surface, keyword FROM keyword_forms"))
        namespaces = {r[1] for r in rows}
    finally:
        conn.close()

    assert rows == [
        ("imdb:tt1", "keywords", "tmdb", "keyword", "bank heist", "2026-01-03T00:00:00+00:00"),
        # The two colliding rows merged into one, keeping the newer fetched_at.
        ("imdb:tt1", "keywords", "tmdb", "keyword", "heist", "2026-01-02T00:00:00+00:00"),
        # No known writer other than tmdb_keywords exists, so the fallback
        # source is the namespace's own name.
        ("imdb:tt2", "mdblist", "mdblist", "list", "top250", "2024-01-01T00:00:00+00:00"),
    ]
    assert "tmdb_keywords" not in namespaces
    assert forms == {"Heists": "heist", "heist": "heist", "bank-heist": "bank heist"}


def test_v10_backfills_source_tmdb_for_both_known_cursor_namespaces(tmp_path: Path) -> None:
    """`enrichment_cursor` gains `source` in the same migration as `enrichment`
    (ADR-0016), but it has two real pre-migration writers, not one: the
    keyword cursor (`namespace='tmdb_keywords'`, renamed to `keywords`) and
    the edge cursor (`namespace='tmdb_edges'`, kept as-is). Both come from
    TMDB, so both must backfill `source='tmdb'` — `tmdb_edges.py`'s own
    `_CURSOR_SOURCE` reads and writes exactly that value, so a cursor row
    left under any other source is invisible to it and gets re-fetched."""
    store = tmp_path / "plexdb.db"
    _v9_store_with_keyword_data(store)
    conn = sqlite3.connect(store)
    try:
        conn.execute(
            "INSERT INTO enrichment_cursor (item_id, namespace, key, fetched_at) "
            "VALUES ('imdb:tt1', 'tmdb_keywords', 'fetched', '2026-01-02T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO enrichment_cursor (item_id, namespace, key, fetched_at) "
            "VALUES ('imdb:tt1', 'tmdb_edges', 'fetched_recommendations', "
            "'2026-01-02T00:00:00+00:00')"
        )
        conn.commit()

        schema._V10(conn)
        conn.commit()

        cursors = conn.execute(
            "SELECT namespace, source, key FROM enrichment_cursor ORDER BY namespace"
        ).fetchall()
    finally:
        conn.close()

    assert cursors == [
        ("keywords", "tmdb", "fetched"),
        ("tmdb_edges", "tmdb", "fetched_recommendations"),
    ]


def test_v10_is_reached_through_init_and_a_second_init_changes_nothing(tmp_path: Path) -> None:
    """Acceptance: migrating a pre-migration fixture reaches the current
    schema with no `tmdb_keywords` rows and a passing health report, and
    running the migration again (via a second `init`, the only way `apply`
    would ever run it twice) changes no rows."""
    store = tmp_path / "plexdb.db"
    _v9_store_with_keyword_data(store)

    was, now = init(store)
    assert (was, now) == (9, schema.SCHEMA_VERSION)

    report = health.inspect(store, tmp_path / "backups")
    assert report.healthy
    assert report.legacy_tmdb_keywords == 0
    assert report.keyword_counts == {"tmdb": 2}

    with open_readonly(store) as conn:
        before_rows = conn.execute(
            "SELECT item_id, namespace, source, key, value, fetched_at FROM enrichment "
            "ORDER BY item_id, namespace, source, key, value"
        ).fetchall()
        before_forms = conn.execute(
            "SELECT surface, keyword FROM keyword_forms ORDER BY surface"
        ).fetchall()

    was2, now2 = init(store)
    assert (was2, now2) == (schema.SCHEMA_VERSION, schema.SCHEMA_VERSION)

    with open_readonly(store) as conn:
        after_rows = conn.execute(
            "SELECT item_id, namespace, source, key, value, fetched_at FROM enrichment "
            "ORDER BY item_id, namespace, source, key, value"
        ).fetchall()
        after_forms = conn.execute(
            "SELECT surface, keyword FROM keyword_forms ORDER BY surface"
        ).fetchall()

    assert after_rows == before_rows
    assert after_forms == before_forms


def test_keyword_pairs_stores_one_ordered_row_per_pair_and_pairs_a_decision_with_its_time(
    tmp_path: Path,
) -> None:
    """ADR-0018. A pair is stored once, smaller keyword first, so asking about
    (b, a) cannot add a second row; and a decision is never recorded without
    the moment it was made."""
    store = tmp_path / "plexdb.db"
    init(store)
    insert = (
        "INSERT INTO keyword_pairs "
        "(keyword_a, keyword_b, jev_score, jev_model, judged_at, decision, decided_at) "
        "VALUES (?, ?, 0.95, 'jev-1.13.0', '2026-10-01T00:00:00Z', ?, ?)"
    )

    with open_store(store) as conn:
        conn.execute(insert, ("hippo", "hippopotamus", None, None))
        conn.execute(insert, ("soviet union", "ussr", "rejected", "2026-10-01T01:00:00Z"))

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, ("hippopotamus", "hippo", None, None))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, ("hippo", "hippopotamus", None, None))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, ("a", "b", "maybe", "2026-10-01T01:00:00Z"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, ("a", "b", "accepted", None))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, ("a", "b", None, "2026-10-01T01:00:00Z"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_score, jev_model, judged_at) "
                "VALUES ('a', 'b', 1.5, 'jev-1.13.0', '2026-10-01T00:00:00Z')"
            )


def test_v13_carries_every_keyword_pair_over_and_accepts_a_refused_pair(tmp_path: Path) -> None:
    """Version 13 rebuilds `keyword_pairs` to make the score optional. A v12 store's
    rows, decisions included, come through unchanged, and a row is either an answer
    (score, model, no error) or a refusal (error, no score, no model)."""
    store = tmp_path / "plexdb.db"
    _store_at(store, 12)
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO keyword_pairs VALUES "
            "('hippo', 'hippopotamus', 0.95, 'jev-1.13.0', '2026-10-01T00:00:00Z', "
            "'accepted', '2026-10-01T01:00:00Z')"
        )
        conn.commit()
        before = conn.execute(
            "SELECT keyword_a, keyword_b, jev_score, jev_model, judged_at, decision, decided_at "
            "FROM keyword_pairs"
        ).fetchall()

    assert init(store) == (12, schema.SCHEMA_VERSION)

    with open_store(store) as conn:
        after = conn.execute(
            "SELECT keyword_a, keyword_b, jev_score, jev_model, judged_at, decision, decided_at "
            "FROM keyword_pairs"
        ).fetchall()
        assert [tuple(r) for r in after] == [tuple(r) for r in before]
        assert conn.execute("PRAGMA index_list(keyword_pairs)").fetchall()

        conn.execute(
            "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_error, judged_at) "
            "VALUES ('a', 'b', 'Jev returned 400', '2026-10-01T00:00:00Z')"
        )
        for bad in (
            "('c', 'd', NULL, NULL, NULL)",  # neither an answer nor a refusal
            "('c', 'd', 0.5, 'jev-1.13.0', 'Jev returned 400')",  # both
            "('c', 'd', 0.5, NULL, NULL)",  # a score with no model
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO keyword_pairs "
                    "(keyword_a, keyword_b, jev_score, jev_model, jev_error, judged_at) "
                    f"VALUES {bad[:-1]}, '2026-10-01T00:00:00Z')"
                )


def _store_at(path: Path, version: int) -> None:
    """A store built by the first `version` migrations alone, stamped at that
    version, so a later step runs against the shape it actually shipped over."""
    conn = sqlite3.connect(path)
    try:
        for migration in schema.MIGRATIONS[:version]:
            if isinstance(migration, str):
                conn.executescript(migration)
            else:
                migration(conn)
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        conn.commit()
    finally:
        conn.close()


def _v13_store_with_enrichment(path: Path) -> None:
    """A v13 store holding one title, one play and two keyword rows — what a
    deployed store looked like before v14."""
    _store_at(path, 13)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'Heat')"
        )
        conn.execute(
            "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) "
            "VALUES ('h1', 'imdb:tt1', 1, 1700000000)"
        )
        conn.executemany(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
            "VALUES ('imdb:tt1', 'keywords', 'tmdb', 'keyword', ?, '2026-10-01T00:00:00Z')",
            [("heist",), ("los angel",)],
        )
        conn.commit()
    finally:
        conn.close()


def test_v14_adds_a_null_rank_to_every_existing_enrichment_row(tmp_path: Path) -> None:
    """Version 14 runs through `init`, so the pre-migration copy and the row-count
    guard on items, plays and enrichment both apply, with no declared shrink."""
    store = tmp_path / "plexdb.db"
    _v13_store_with_enrichment(store)

    result = migrate(store, tmp_path / "backups")

    assert (result.was, result.now) == (13, schema.SCHEMA_VERSION)
    assert result.counts_after == result.counts_before

    with open_readonly(store) as conn:
        assert [
            tuple(r) for r in conn.execute("SELECT value, rank FROM enrichment ORDER BY value")
        ] == [
            ("heist", None),
            ("los angel", None),
        ]
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM plays").fetchone()[0] == 1
    assert result.backup == tmp_path / "backups" / f"plexdb.pre-v{schema.SCHEMA_VERSION}.db"
    assert result.backup.exists()


def test_v16_makes_every_wikidata_title_due_and_keeps_its_rows(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    _store_at(store, 15)
    conn = sqlite3.connect(store)
    try:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'x')")
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
            "VALUES ('imdb:tt1', 'keywords', 'wikidata', 'keyword', 'pari', '2026-10-01T00:00:00Z')"
        )
        conn.executemany(
            "INSERT INTO enrichment_cursor (item_id, namespace, source, key, fetched_at) "
            "VALUES ('imdb:tt1', 'keywords', ?, 'fetched', '2026-10-01T00:00:00Z')",
            [("wikidata",), ("tmdb",)],
        )
        conn.commit()
    finally:
        conn.close()

    result = migrate(store, tmp_path / "backups")

    assert (result.was, result.now) == (15, schema.SCHEMA_VERSION)
    with open_readonly(store) as conn:
        cursors = conn.execute("SELECT source FROM enrichment_cursor").fetchall()
        assert [r[0] for r in cursors] == ["tmdb"]
        assert conn.execute("SELECT value FROM enrichment").fetchone()[0] == "pari"


def test_v17_renames_a_stem_with_one_new_form_and_refetches_one_that_splits(
    tmp_path: Path,
) -> None:
    """`christma` has one spelling, so it becomes `christmas` and its pair moves
    with it; `anim` was both Animals and Anime, so its row stays and its title is
    fetched again. Nothing else is lost."""
    store = tmp_path / "plexdb.db"
    _store_at(store, 16)
    at = "2026-10-01T00:00:00Z"
    conn = sqlite3.connect(store)
    try:
        conn.executemany(
            "INSERT INTO items (item_id, type, title) VALUES (?, 'movie', ?)",
            [("imdb:tt1", "Elf"), ("imdb:tt2", "Akira")],
        )
        conn.executemany(
            "INSERT INTO keyword_forms (surface, keyword) VALUES (?, ?)",
            [
                ("Christmas", "christma"),
                ("Christmas film", "christma film"),
                ("Animals", "anim"),
                ("Anime", "anim"),
            ],
        )
        conn.executemany(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
            "VALUES (?, 'keywords', ?, 'keyword', ?, ?)",
            [
                ("imdb:tt1", "tmdb", "christma", at),
                ("imdb:tt1", "wikidata", "christma film", at),
                ("imdb:tt2", "tmdb", "anim", at),
            ],
        )
        conn.executemany(
            "INSERT INTO enrichment_cursor (item_id, namespace, source, key, fetched_at) "
            "VALUES (?, 'keywords', ?, 'fetched', ?)",
            [("imdb:tt1", "tmdb", at), ("imdb:tt2", "tmdb", at)],
        )
        conn.execute(
            "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_score, jev_model, judged_at) "
            "VALUES ('christma', 'christma film', 0.09, 'jev-1', ?)",
            (at,),
        )
        conn.commit()
    finally:
        conn.close()

    result = migrate(store, tmp_path / "backups")

    assert (result.was, result.now) == (16, schema.SCHEMA_VERSION)
    assert result.counts_after == result.counts_before
    with open_readonly(store) as conn:
        values = {tuple(r) for r in conn.execute("SELECT item_id, value FROM enrichment")}
        assert values == {
            ("imdb:tt1", "christmas"),
            ("imdb:tt1", "christmas film"),
            ("imdb:tt2", "anim"),
        }
        cursors = [r[0] for r in conn.execute("SELECT item_id FROM enrichment_cursor")]
        assert cursors == ["imdb:tt1"]
        pairs = [tuple(r) for r in conn.execute("SELECT keyword_a, keyword_b FROM keyword_pairs")]
        assert pairs == [("christmas", "christmas film")]
        forms = dict(conn.execute("SELECT surface, keyword FROM keyword_forms").fetchall())
        assert forms["Christmas"] == "christmas"
        assert forms["Anime"] == "anime" and forms["Animals"] == "animal"
        surfaces = {tuple(r) for r in conn.execute("SELECT item_id, surface FROM keyword_surfaces")}
        assert surfaces == {
            ("imdb:tt1", "Christmas"),
            ("imdb:tt1", "Christmas film"),
            ("imdb:tt2", "anim"),
        }


def test_keyword_roles_holds_a_stated_role_a_score_or_a_refusal_for_the_closed_role_set(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    init(store)
    insert = (
        "INSERT INTO keyword_roles (keyword, role, source, score, model, error, stated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, '2026-10-03T00:00:00Z')"
    )
    with open_store(store) as conn:
        conn.execute(insert, ("los angel", "region", "wikidata", None, None, None))
        conn.execute(insert, ("los angel", "region", "jev", 0.97, "jev-1.13.0", None))
        conn.execute(insert, ("noir", "tone", "jev", None, None, "Jev returned 422"))
        for bad in (
            ("heist", "mood", "jev", 0.5, "jev-1.13.0", None),  # not a role
            ("heist", "theme", "jev", 1.5, "jev-1.13.0", None),  # score out of range
            ("heist", "theme", "jev", 0.5, "jev-1.13.0", "Jev returned 400"),  # both
            ("los angel", "region", "jev", 0.1, "jev-1.13.0", None),  # same source twice
            ("heist", "theme", "jev", 0.5, None, None),  # a score with no model
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(insert, bad)


def test_a_role_decision_is_one_row_per_keyword_and_role_never_per_source(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    init(store)
    insert = (
        "INSERT INTO keyword_role_decisions (keyword, role, decision, decided_at) "
        "VALUES (?, ?, ?, '2026-10-03T00:00:00Z')"
    )
    with open_store(store) as conn:
        conn.execute(insert, ("los angel", "region", "accepted"))
        conn.execute(insert, ("los angel", "era", "rejected"))
        for bad in (
            ("los angel", "region", "rejected"),  # a second decision on the same pair
            ("los angel", "tone", "maybe"),  # not a decision
            ("los angel", "mood", "accepted"),  # not a role
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(insert, bad)


def _collection_queries() -> list[str]:
    """The SQL blocks under schema.md's "Reading the store to build collections",
    in the order the page lists them."""
    page = (Path(__file__).parent.parent / "docs" / "schema.md").read_text()
    section = page.split("## Reading the store to build collections", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"```sql\n(.*?)```", section, re.S)


def test_every_collection_query_in_schema_md_returns_what_it_says(tmp_path: Path) -> None:
    """schema.md publishes these queries to consumers, so each one runs here over
    a store seeded to give it a known answer."""
    store = tmp_path / "plexdb.db"
    init(store)
    with open_store(store) as conn:
        conn.executemany(
            "INSERT INTO items (item_id, type, title) VALUES (?, 'movie', ?)",
            [("imdb:tt0113277", "Heat"), ("imdb:tt0072890", "Dog Day Afternoon")],
        )
        conn.executemany(
            "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
            "VALUES (?, ?, '1', '2026-10-03T00:00:00Z')",
            [("85450", "imdb:tt0113277"), ("85451", "imdb:tt0113277")],
        )
        conn.executemany(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, '2026-10-03T00:00:00Z')",
            [
                ("imdb:tt0113277", "keywords", "tmdb", "keyword", "heist"),
                ("imdb:tt0113277", "keywords", "tmdb", "keyword", "bank robberi"),
                ("imdb:tt0113277", "keywords", "wikidata", "keyword", "bank robberi"),
                ("imdb:tt0072890", "keywords", "tmdb", "keyword", "heist"),
                ("imdb:tt0072890", "keywords", "tmdb", "keyword", "hostag"),
                ("imdb:tt0113277", "ratings", "mdblist", "imdb", "8.3"),
            ],
        )
        conn.executemany(
            "INSERT INTO keyword_roles (keyword, role, source, score, model, error, stated_at) "
            "VALUES (?, 'region', ?, ?, ?, ?, '2026-10-03T00:00:00Z')",
            [
                ("los angel", "wikidata", None, None, None),  # stated
                ("tokyo", "jev", 0.95, "jev-1.13.0", None),  # over the threshold
                ("paris", "jev", 0.3, "jev-1.13.0", None),  # under it
                ("heist", "jev", 0.9, "jev-1.13.0", None),  # over it, but rejected
                ("noir", "jev", None, None, "Jev returned 422"),  # refused
            ],
        )
        conn.executemany(
            "INSERT INTO keyword_role_decisions (keyword, role, decision, decided_at) "
            "VALUES (?, 'region', ?, '2026-10-03T00:00:00Z')",
            [("heist", "rejected"), ("paris", "accepted")],
        )
        conn.commit()

        queries = _collection_queries()
        assert len(queries) == 5
        combo, cooccur, rating_keys, roles, ratings = (
            [tuple(r) for r in conn.execute(q)] for q in queries
        )

    assert combo == [("imdb:tt0113277", "Heat", None)]
    assert cooccur == [("bank robberi", 1), ("hostag", 1)]
    assert sorted(rating_keys) == [("85450", "1"), ("85451", "1")]
    assert roles == [("los angel",), ("paris",), ("tokyo",)]
    assert ratings == [("imdb", "8.3", "mdblist")]
