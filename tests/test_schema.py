"""The store the writer builds carries the constraints every writer relies on,
and the version and read shape this package compares a store against.

The chain that builds it, and each migration's handling of an older store, is
tested in Rust (`crates/plexdb-schema/tests/`); these build their store with it.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from fixture_store import init

from plexdb import schema
from plexdb.store import open_readonly, open_store


def test_a_store_the_writer_builds_carries_this_packages_version_and_read_shape(
    tmp_path: Path,
) -> None:
    """`plexdb check` judges a store against `schema.SCHEMA_VERSION`, and
    `plexdb-reader` refuses any store whose `reader_shape` is not its own, so
    both numbers must be the ones the Rust chain stamps."""
    store = tmp_path / "plexdb.db"
    init(store)
    with open_readonly(store) as conn:
        assert schema.current_version(conn) == schema.SCHEMA_VERSION
        assert [tuple(r) for r in conn.execute("SELECT version FROM reader_shape")] == [
            (schema.READER_SHAPE,)
        ]


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


def test_a_movie_and_a_show_hold_the_same_tmdb_number(tmp_path: Path) -> None:
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
