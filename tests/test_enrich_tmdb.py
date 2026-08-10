"""Fetching TMDB keywords into the `tmdb_keywords` enrichment namespace, cached.

Drives `enrich_tmdb_keywords` against the in-memory `TMDbSource` fakes in
`tmdb_fixtures.py` — never a live server. The caching discipline is the
substance of issue #4: `FailOnRepeatSource` fails the test outright the
instant a title is asked for twice, which is a stronger guarantee than
inspecting a stored timestamp after the fact.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tmdb_fixtures import FailOnRepeatSource, FakeTMDbSource

from plexdb.enrich_tmdb import NAMESPACE, EnrichStats, enrich_tmdb_keywords, wipe_namespace
from plexdb.store import init as init_store
from plexdb.store import open_store

MOVIE_ID = "imdb:tt0468569"  # The Dark Knight — arbitrary item_id shape
SHOW_ID = "imdb:tt0903747"  # Breaking Bad


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


def test_keywords_land_as_enrichment_rows_with_a_fetched_at(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=MOVIE_ID, item_type="movie", title="The Dark Knight", tmdb_id="155")
        source = FakeTMDbSource(keywords_by_id={("155", "movie"): ["superhero", "gotham city"]})

        stats = enrich_tmdb_keywords(conn, source)

        rows = _rows(
            conn,
            "SELECT key, value, fetched_at FROM enrichment "
            "WHERE item_id = ? AND namespace = ? AND key = 'keyword' ORDER BY value",
            (MOVIE_ID, NAMESPACE),
        )
        assert [(r["key"], r["value"]) for r in rows] == [
            ("keyword", "gotham city"),
            ("keyword", "superhero"),
        ]
        for row in rows:
            assert row["fetched_at"]  # every keyword row carries its own timestamp

    assert stats == EnrichStats(
        titles_seen=1,
        titles_fetched=1,
        titles_cached=0,
        titles_skipped_no_tmdb_id=0,
        keywords_written=2,
    )


def test_a_source_that_fails_on_a_repeat_ask_proves_the_cache_holds(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=MOVIE_ID, item_type="movie", title="The Dark Knight", tmdb_id="155")
        _seed(conn, item_id=SHOW_ID, item_type="show", title="Breaking Bad", tmdb_id="1396")
        source = FailOnRepeatSource(
            keywords_by_id={
                ("155", "movie"): ["superhero"],
                ("1396", "tv"): ["drug dealer"],
            }
        )

        first = enrich_tmdb_keywords(conn, source)
        # A second sweep over the same, unchanged store must not ask `source`
        # again for either title — `FailOnRepeatSource` raises if it does.
        second = enrich_tmdb_keywords(conn, source)

    assert first.titles_fetched == 2
    assert first.titles_cached == 0
    assert second.titles_fetched == 0
    assert second.titles_cached == 2
    assert len(source.calls) == 2


def test_a_row_past_its_staleness_threshold_is_refetched_a_row_inside_it_is_not(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=MOVIE_ID, item_type="movie", title="The Dark Knight", tmdb_id="155")
        _seed(conn, item_id=SHOW_ID, item_type="show", title="Breaking Bad", tmdb_id="1396")
        source = FakeTMDbSource(
            keywords_by_id={
                ("155", "movie"): ["superhero"],
                ("1396", "tv"): ["drug dealer"],
            }
        )
        enrich_tmdb_keywords(conn, source, stale_days=30)

        # Backdate the movie's fetch past the threshold; leave the show's
        # alone (still fresh). Only the movie should be re-fetched next.
        stale_at = (datetime.now(UTC) - timedelta(days=31)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE enrichment SET fetched_at = ? WHERE item_id = ? AND namespace = ?",
            (stale_at, MOVIE_ID, NAMESPACE),
        )
        conn.commit()

        second = enrich_tmdb_keywords(conn, source, stale_days=30)

    assert second.titles_fetched == 1
    assert second.titles_cached == 1
    assert source.calls == [("155", "movie"), ("1396", "tv"), ("155", "movie")]


def test_wiping_one_namespace_leaves_another_namespace_untouched(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=MOVIE_ID, item_type="movie", title="The Dark Knight", tmdb_id="155")
        source = FakeTMDbSource(keywords_by_id={("155", "movie"): ["superhero"]})
        enrich_tmdb_keywords(conn, source)
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) "
            "VALUES (?, 'other_source', 'k', 'v', '2020-01-01T00:00:00+00:00')",
            (MOVIE_ID,),
        )
        conn.commit()

        removed = wipe_namespace(conn, NAMESPACE)

        remaining_namespaces = {
            row["namespace"] for row in _rows(conn, "SELECT DISTINCT namespace FROM enrichment")
        }

    assert removed > 0
    assert remaining_namespaces == {"other_source"}


def test_a_title_with_no_tmdb_id_is_skipped_and_reported_not_errored(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(
            conn,
            item_id="tvdb:12345",
            item_type="show",
            title="TVDB-Only Anime",
            tmdb_id=None,
        )
        source = FailOnRepeatSource()  # any call at all is a bug here

        stats = enrich_tmdb_keywords(conn, source)

    assert stats.titles_seen == 1
    assert stats.titles_skipped_no_tmdb_id == 1
    assert stats.titles_fetched == 0
    assert source.calls == []


def test_a_movie_with_zero_keywords_is_still_cached_not_reasked_forever(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=MOVIE_ID, item_type="movie", title="Obscure Movie", tmdb_id="424783")
        source = FailOnRepeatSource(keywords_by_id={})  # 424783 -> no keywords, both sweeps

        first = enrich_tmdb_keywords(conn, source)
        second = enrich_tmdb_keywords(conn, source)

    assert first.titles_fetched == 1
    assert first.keywords_written == 0
    assert second.titles_cached == 1
    assert len(source.calls) == 1


def test_episodes_are_not_enriched(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=SHOW_ID, item_type="show", title="Breaking Bad", tmdb_id="1396")
        conn.execute(
            "INSERT INTO items (item_id, type, title, show_item_id) VALUES (?, 'episode', ?, ?)",
            ("imdb:tt0959621", "Pilot", SHOW_ID),
        )
        conn.commit()
        source = FakeTMDbSource(keywords_by_id={("1396", "tv"): ["drug dealer"]})

        stats = enrich_tmdb_keywords(conn, source)

    # Only the show is counted; the episode is invisible to this sweep.
    assert stats.titles_seen == 1
    assert source.calls == [("1396", "tv")]
