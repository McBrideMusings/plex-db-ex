"""Fetching TMDB recommendations and similar titles into the `edges` table.

Drives `refresh_tmdb_edges` against the in-memory `TMDbSource` fakes in
`tmdb_fixtures.py` — never a live server. Mirrors `test_enrich_tmdb.py`'s
shape: same caching-discipline proof (`FailOnRepeatSource`), same
consecutive-failure abort, plus the edges-specific rules issue #6 is about —
replace-wholesale on re-pull, rank round-tripping, and dropping a
recommendation that points outside the library rather than inventing an id
for it.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tmdb_fixtures import FailOnRepeatSource, FakeTMDbSource

from plexdb.errors import TMDbError
from plexdb.store import init as init_store
from plexdb.store import open_store
from plexdb.tmdb_edges import (
    RECOMMENDATIONS_EDGE_TYPE,
    SIMILAR_EDGE_TYPE,
    EdgeStats,
    refresh_tmdb_edges,
    wipe_edge_type,
)

DARK_KNIGHT = "imdb:tt0468569"
BATMAN_BEGINS = "imdb:tt0372784"
BREAKING_BAD = "imdb:tt0903747"
BETTER_CALL_SAUL = "imdb:tt3032476"


def _open(store: Path) -> Any:
    init_store(store)
    return open_store(store)


def _seed(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    item_type: str,
    title: str,
    tmdb_id: str | None,
) -> None:
    conn.execute(
        "INSERT INTO items (item_id, type, title) VALUES (?, ?, ?)",
        (item_id, item_type, title),
    )
    if tmdb_id is not None:
        conn.execute(
            "INSERT INTO external_ids (item_id, ns, value) VALUES (?, 'tmdb', ?)",
            (item_id, tmdb_id),
        )
    conn.commit()


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    return list(conn.execute(sql, params).fetchall())


def test_recommendations_land_as_ranked_edges_to_titles_already_in_the_library(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", tmdb_id="155")
        _seed(
            conn,
            item_id=BATMAN_BEGINS,
            item_type="movie",
            title="Batman Begins",
            tmdb_id="272",
        )
        source = FakeTMDbSource(
            recommendations_by_id={("155", "movie"): ["272", "999999"]},
        )

        stats = refresh_tmdb_edges(conn, source)

        rows = [
            tuple(row)
            for row in _rows(
                conn,
                "SELECT from_id, to_id, edge_type, rank FROM edges WHERE from_id = ?",
                (DARK_KNIGHT,),
            )
        ]

    # "999999" has no local item, so only the Batman Begins edge lands, at
    # its original rank (1) — not renumbered once the unresolvable entry is
    # dropped.
    assert rows == [(DARK_KNIGHT, BATMAN_BEGINS, RECOMMENDATIONS_EDGE_TYPE, 1)]
    rec_stats = stats[RECOMMENDATIONS_EDGE_TYPE]
    # Both seeded titles are candidates in their own right — Batman Begins is
    # asked for its own recommendations too (TMDB has none scripted for it),
    # so `titles_fetched` counts both even though only one produced an edge.
    assert rec_stats == EdgeStats(
        titles_seen=2,
        titles_fetched=2,
        titles_cached=0,
        titles_skipped_no_tmdb_id=0,
        titles_failed=0,
        edges_written=1,
        edges_skipped_not_in_library=1,
    )


def test_similar_lands_as_its_own_distinct_edge_type(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=BREAKING_BAD, item_type="show", title="Breaking Bad", tmdb_id="1396")
        _seed(
            conn,
            item_id=BETTER_CALL_SAUL,
            item_type="show",
            title="Better Call Saul",
            tmdb_id="60059",
        )
        source = FakeTMDbSource(similar_by_id={("1396", "tv"): ["60059"]})

        stats = refresh_tmdb_edges(conn, source)

        edge_types = {
            row["edge_type"]
            for row in _rows(
                conn, "SELECT DISTINCT edge_type FROM edges WHERE from_id = ?", (BREAKING_BAD,)
            )
        }

    assert edge_types == {SIMILAR_EDGE_TYPE}
    assert stats[SIMILAR_EDGE_TYPE].edges_written == 1
    assert stats[RECOMMENDATIONS_EDGE_TYPE].edges_written == 0


def test_rank_round_trips_exactly_as_the_source_gave_it(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", tmdb_id="155")
        _seed(conn, item_id=BATMAN_BEGINS, item_type="movie", title="Batman Begins", tmdb_id="272")
        _seed(conn, item_id=BREAKING_BAD, item_type="show", title="Breaking Bad", tmdb_id="1396")
        # Batman Begins ranked #2 behind Breaking Bad's tmdb id at #1 — an
        # implausible recommendation, but the point is the position, not the
        # content, and both targets exist locally so both land.
        source = FakeTMDbSource(recommendations_by_id={("155", "movie"): ["1396", "272"]})

        refresh_tmdb_edges(conn, source)

        rows = [
            tuple(row)
            for row in _rows(
                conn,
                "SELECT to_id, rank FROM edges WHERE from_id = ? AND edge_type = ? ORDER BY rank",
                (DARK_KNIGHT, RECOMMENDATIONS_EDGE_TYPE),
            )
        ]

    assert rows == [(BREAKING_BAD, 1), (BATMAN_BEGINS, 2)]


def test_a_source_that_fails_on_a_repeat_ask_proves_the_cache_holds(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", tmdb_id="155")
        _seed(conn, item_id=BATMAN_BEGINS, item_type="movie", title="Batman Begins", tmdb_id="272")
        source = FailOnRepeatSource(
            recommendations_by_id={("155", "movie"): ["272"]},
            similar_by_id={("155", "movie"): ["272"]},
        )

        first = refresh_tmdb_edges(conn, source)
        second = refresh_tmdb_edges(conn, source)

    # Both seeded titles are candidates, so the first pass fetches both
    # (2 calls per method); the second pass finds everything cached and asks
    # `source` for nothing new — `FailOnRepeatSource` would raise if it did.
    assert first[RECOMMENDATIONS_EDGE_TYPE].titles_fetched == 2
    assert second[RECOMMENDATIONS_EDGE_TYPE].titles_fetched == 0
    assert second[RECOMMENDATIONS_EDGE_TYPE].titles_cached == 2
    assert second[SIMILAR_EDGE_TYPE].titles_cached == 2
    assert len(source.recommendation_calls) == 2
    assert len(source.similar_calls) == 2


def test_a_title_whose_recommendations_are_all_outside_the_library_is_still_cached(
    tmp_path: Path,
) -> None:
    """The zero-local-match case is the edges analogue of `enrich_tmdb.py`'s
    zero-keyword sentinel: no `edges` row is written, so without a separate
    cursor this title would be re-asked every sweep forever."""
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", tmdb_id="155")
        source = FailOnRepeatSource(recommendations_by_id={("155", "movie"): ["999999"]})

        first = refresh_tmdb_edges(conn, source)
        second = refresh_tmdb_edges(conn, source)

        edge_count = _rows(conn, "SELECT * FROM edges")

    assert first[RECOMMENDATIONS_EDGE_TYPE].titles_fetched == 1
    assert first[RECOMMENDATIONS_EDGE_TYPE].edges_skipped_not_in_library == 1
    assert second[RECOMMENDATIONS_EDGE_TYPE].titles_cached == 1
    assert edge_count == []
    assert len(source.recommendation_calls) == 1


def test_a_re_pull_replaces_the_set_an_edge_the_source_dropped_disappears(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", tmdb_id="155")
        _seed(conn, item_id=BATMAN_BEGINS, item_type="movie", title="Batman Begins", tmdb_id="272")
        _seed(conn, item_id=BREAKING_BAD, item_type="show", title="Breaking Bad", tmdb_id="1396")
        source = FakeTMDbSource(recommendations_by_id={("155", "movie"): ["272", "1396"]})
        refresh_tmdb_edges(conn, source, stale_days=30)

        # Backdate the cursor so the next sweep is forced to re-fetch, and
        # TMDB now drops Breaking Bad and gains nothing new.
        stale_at = (datetime.now(UTC) - timedelta(days=31)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE enrichment SET fetched_at = ? WHERE namespace = 'tmdb_edges'",
            (stale_at,),
        )
        conn.commit()
        source.recommendations_by_id[("155", "movie")] = ["272"]

        refresh_tmdb_edges(conn, source, stale_days=30)

        to_ids = {
            row["to_id"]
            for row in _rows(
                conn,
                "SELECT to_id FROM edges WHERE from_id = ? AND edge_type = ?",
                (DARK_KNIGHT, RECOMMENDATIONS_EDGE_TYPE),
            )
        }

    assert to_ids == {BATMAN_BEGINS}


def test_a_re_pull_adds_an_edge_the_source_gained(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", tmdb_id="155")
        _seed(conn, item_id=BATMAN_BEGINS, item_type="movie", title="Batman Begins", tmdb_id="272")
        _seed(conn, item_id=BREAKING_BAD, item_type="show", title="Breaking Bad", tmdb_id="1396")
        source = FakeTMDbSource(recommendations_by_id={("155", "movie"): ["272"]})
        refresh_tmdb_edges(conn, source, stale_days=30)

        stale_at = (datetime.now(UTC) - timedelta(days=31)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE enrichment SET fetched_at = ? WHERE namespace = 'tmdb_edges'",
            (stale_at,),
        )
        conn.commit()
        source.recommendations_by_id[("155", "movie")] = ["272", "1396"]

        refresh_tmdb_edges(conn, source, stale_days=30)

        to_ids = {
            row["to_id"]
            for row in _rows(
                conn,
                "SELECT to_id FROM edges WHERE from_id = ? AND edge_type = ?",
                (DARK_KNIGHT, RECOMMENDATIONS_EDGE_TYPE),
            )
        }

    assert to_ids == {BATMAN_BEGINS, BREAKING_BAD}


def test_both_directions_are_queryable_without_string_parsing(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", tmdb_id="155")
        _seed(conn, item_id=BATMAN_BEGINS, item_type="movie", title="Batman Begins", tmdb_id="272")
        source = FakeTMDbSource(recommendations_by_id={("155", "movie"): ["272"]})
        refresh_tmdb_edges(conn, source)

        forward = _rows(
            conn,
            "SELECT to_id FROM edges WHERE from_id = ? AND edge_type = ?",
            (DARK_KNIGHT, RECOMMENDATIONS_EDGE_TYPE),
        )
        reverse = _rows(
            conn,
            "SELECT from_id FROM edges WHERE to_id = ? AND edge_type = ?",
            (BATMAN_BEGINS, RECOMMENDATIONS_EDGE_TYPE),
        )

    assert [r["to_id"] for r in forward] == [BATMAN_BEGINS]
    assert [r["from_id"] for r in reverse] == [DARK_KNIGHT]


def test_a_title_with_no_tmdb_id_is_skipped_and_reported_not_errored(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id="tvdb:12345", item_type="show", title="TVDB-Only Anime", tmdb_id=None)
        source = FailOnRepeatSource()  # any call at all is a bug here

        stats = refresh_tmdb_edges(conn, source)

    assert stats[RECOMMENDATIONS_EDGE_TYPE].titles_seen == 1
    assert stats[RECOMMENDATIONS_EDGE_TYPE].titles_skipped_no_tmdb_id == 1
    assert source.recommendation_calls == []
    assert source.similar_calls == []


def test_a_movie_recommending_itself_is_dropped_not_stored_as_a_self_loop(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", tmdb_id="155")
        source = FakeTMDbSource(recommendations_by_id={("155", "movie"): ["155"]})

        stats = refresh_tmdb_edges(conn, source)

        rows = _rows(conn, "SELECT * FROM edges")

    assert rows == []
    assert stats[RECOMMENDATIONS_EDGE_TYPE].edges_skipped_not_in_library == 1


def test_two_tmdb_ids_resolving_to_the_same_local_item_do_not_crash_the_sweep(
    tmp_path: Path,
) -> None:
    """A target item carrying two `tmdb` external_ids rows is schema-legal
    (`external_ids`' primary key is `(ns, value)`, not `(item_id, ns)`). If a
    recommendation list names both of that item's tmdb ids, the unguarded
    insert loop would try to write the same `(from_id, to_id, edge_type)`
    twice and crash on the `edges` primary key — this proves the dedup holds
    and only the first-seen rank survives."""
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", tmdb_id="155")
        _seed(conn, item_id=BATMAN_BEGINS, item_type="movie", title="Batman Begins", tmdb_id="272")
        # A second, stale/duplicate tmdb id landing on the same local item —
        # both resolve to BATMAN_BEGINS.
        conn.execute(
            "INSERT INTO external_ids (item_id, ns, value) VALUES (?, 'tmdb', ?)",
            (BATMAN_BEGINS, "99999"),
        )
        conn.commit()
        source = FakeTMDbSource(recommendations_by_id={("155", "movie"): ["272", "99999"]})

        stats = refresh_tmdb_edges(conn, source)  # must not raise sqlite3.IntegrityError

        rows = [
            tuple(row)
            for row in _rows(
                conn,
                "SELECT to_id, rank FROM edges WHERE from_id = ? AND edge_type = ?",
                (DARK_KNIGHT, RECOMMENDATIONS_EDGE_TYPE),
            )
        ]

    # Only one edge lands, at the rank its first-seen tmdb id carried.
    assert rows == [(BATMAN_BEGINS, 1)]
    assert stats[RECOMMENDATIONS_EDGE_TYPE].edges_written == 1


def test_three_consecutive_failures_abort_with_the_tripping_error(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        for i in range(1, 6):
            _seed(
                conn,
                item_id=f"imdb:tt{i:07d}",
                item_type="movie",
                title=f"Movie {i}",
                tmdb_id=str(i),
            )
        source = FakeTMDbSource(fail_recommendation_calls={1, 2, 3})

        with pytest.raises(TMDbError) as excinfo:
            refresh_tmdb_edges(conn, source)

    message = str(excinfo.value)
    assert "3 consecutive failures" in message
    assert source.recommendation_calls == [
        ("1", "movie"),
        ("2", "movie"),
        ("3", "movie"),
    ]


def test_rewipe_removes_edges_and_cursor_for_one_edge_type_only(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", tmdb_id="155")
        _seed(conn, item_id=BATMAN_BEGINS, item_type="movie", title="Batman Begins", tmdb_id="272")
        source = FakeTMDbSource(
            recommendations_by_id={("155", "movie"): ["272"]},
            similar_by_id={("155", "movie"): ["272"]},
        )
        refresh_tmdb_edges(conn, source)

        edges_removed, cursor_removed = wipe_edge_type(conn, RECOMMENDATIONS_EDGE_TYPE)

        remaining_edge_types = {
            row["edge_type"] for row in _rows(conn, "SELECT DISTINCT edge_type FROM edges")
        }
        remaining_cursor_keys = {
            row["key"]
            for row in _rows(conn, "SELECT key FROM enrichment WHERE namespace = 'tmdb_edges'")
        }

    assert edges_removed == 1
    # Both seeded titles got a recommendations cursor row (Batman Begins'
    # own recommendations call just produced zero locally-resolvable edges),
    # so wiping the edge type clears both cursor rows, not just one.
    assert cursor_removed == 2
    assert remaining_edge_types == {SIMILAR_EDGE_TYPE}
    assert remaining_cursor_keys == {"_fetched_similar"}


def test_a_failure_partway_through_leaves_the_previous_set_intact(tmp_path: Path) -> None:
    """A title already committed in this sweep keeps its edges even when a
    *later* title's fetch raises — atomicity is per `(from_id, edge_type)`,
    not per sweep."""
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", tmdb_id="155")
        _seed(conn, item_id=BATMAN_BEGINS, item_type="movie", title="Batman Begins", tmdb_id="272")
        _seed(
            conn, item_id="imdb:tt9999997", item_type="movie", title="Doomed Movie A", tmdb_id="997"
        )
        _seed(
            conn, item_id="imdb:tt9999998", item_type="movie", title="Doomed Movie B", tmdb_id="998"
        )
        # Dark Knight's own recommendations call succeeds and commits first;
        # the next three titles' calls all fail, tripping the 3-consecutive
        # abort on the fourth call overall.
        source = FakeTMDbSource(
            recommendations_by_id={("155", "movie"): ["272"]},
            fail_recommendation_calls={2, 3, 4},
        )

        with pytest.raises(TMDbError):
            refresh_tmdb_edges(conn, source)

        surviving = _rows(
            conn,
            "SELECT to_id FROM edges WHERE from_id = ? AND edge_type = ?",
            (DARK_KNIGHT, RECOMMENDATIONS_EDGE_TYPE),
        )

    assert [r["to_id"] for r in surviving] == [BATMAN_BEGINS]
