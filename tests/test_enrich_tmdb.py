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

import pytest
from tmdb_fixtures import FailOnRepeatSource, FakeTMDbSource

from plexdb.enrich_tmdb import NAMESPACE, EnrichStats, enrich_tmdb_keywords, wipe_namespace
from plexdb.errors import TMDbError
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
            "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
            "VALUES (?, 'tmdb', ?, ?, '2026-01-01T00:00:00+00:00')",
            (item_id, tmdb_id, item_type),
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
        # Snowball's English stemmer reduces "city" to "citi" — expected, not a
        # bug: stemming trades a real word for one that collapses with its
        # plural/inflected forms, which is the whole point of normalizing.
        assert [(r["key"], r["value"]) for r in rows] == [
            ("keyword", "gotham citi"),
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
            "UPDATE enrichment_cursor SET fetched_at = ? WHERE item_id = ? AND namespace = ?",
            (stale_at, MOVIE_ID, NAMESPACE),
        )
        conn.commit()

        second = enrich_tmdb_keywords(conn, source, stale_days=30)

    assert second.titles_fetched == 1
    assert second.titles_cached == 1
    assert source.calls == [("155", "movie"), ("1396", "tv"), ("155", "movie")]


def test_wiping_this_source_leaves_another_namespace_untouched(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=MOVIE_ID, item_type="movie", title="The Dark Knight", tmdb_id="155")
        source = FakeTMDbSource(keywords_by_id={("155", "movie"): ["superhero"]})
        enrich_tmdb_keywords(conn, source)
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
            "VALUES (?, 'other_namespace', 'x', 'k', 'v', '2020-01-01T00:00:00+00:00')",
            (MOVIE_ID,),
        )
        conn.commit()

        removed = wipe_namespace(conn)

        remaining_namespaces = {
            row["namespace"] for row in _rows(conn, "SELECT DISTINCT namespace FROM enrichment")
        }

    assert removed > 0
    assert remaining_namespaces == {"other_namespace"}


def test_wiping_this_source_leaves_another_sources_keyword_on_the_item(tmp_path: Path) -> None:
    """Acceptance: two sources both list `heist` on an item; refreshing one
    source deletes only its own row (ADR-0016 scopes a refresh to `namespace =
    'keywords' AND source = <that source>`), so the item still carries `heist`
    through the other source."""
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=MOVIE_ID, item_type="movie", title="The Dark Knight", tmdb_id="155")
        source = FakeTMDbSource(keywords_by_id={("155", "movie"): ["heist"]})
        enrich_tmdb_keywords(conn, source)
        # A second source lists the same stored keyword on the same item.
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
            "VALUES (?, 'keywords', 'mdblist', 'keyword', 'heist', '2020-01-01T00:00:00+00:00')",
            (MOVIE_ID,),
        )
        conn.commit()

        removed = wipe_namespace(conn)

        remaining = _rows(
            conn,
            "SELECT source, value FROM enrichment WHERE item_id = ? AND namespace = 'keywords'",
            (MOVIE_ID,),
        )

    assert removed > 0
    assert [(r["source"], r["value"]) for r in remaining] == [("mdblist", "heist")]


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


def _seed_n_movies(conn: sqlite3.Connection, n: int) -> list[str]:
    """Seed `n` movies, each with tmdb id `str(i)`, returning the item_ids in
    insertion order — the order the sweep visits them, the same rowid-order
    assumption the staleness test above makes via `source.calls`."""
    item_ids = [f"imdb:tt{i:07d}" for i in range(1, n + 1)]
    for i, item_id in enumerate(item_ids, start=1):
        _seed(conn, item_id=item_id, item_type="movie", title=f"Movie {i}", tmdb_id=str(i))
    return item_ids


def test_a_failed_title_is_counted_and_the_sweep_continues(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        failed_id, _ = _seed_n_movies(conn, 2)
        # The first title fails; the second succeeds.
        source = FakeTMDbSource(keywords_by_id={("2", "movie"): ["ok"]}, fail_calls={1})

        stats = enrich_tmdb_keywords(conn, source)

        failed_rows = _rows(
            conn,
            "SELECT * FROM enrichment WHERE item_id = ? AND namespace = ?",
            (failed_id, NAMESPACE),
        )

    assert stats.titles_seen == 2
    assert stats.titles_failed == 1
    assert stats.titles_fetched == 1
    # Nothing was cached for the failed title, so a re-run retries it.
    assert failed_rows == []


def test_two_failures_then_a_success_resets_the_consecutive_counter(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_n_movies(conn, 5)
        # fail, fail, succeed, fail, fail — never 3 in a row, so this must
        # not abort even though 4 of 5 titles fail overall.
        source = FakeTMDbSource(keywords_by_id={("3", "movie"): ["ok"]}, fail_calls={1, 2, 4, 5})

        stats = enrich_tmdb_keywords(conn, source)

    assert stats.titles_seen == 5
    assert stats.titles_failed == 4
    assert stats.titles_fetched == 1
    assert len(source.calls) == 5


def test_three_consecutive_failures_abort_with_counts_and_the_tripping_error(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_n_movies(conn, 5)
        source = FakeTMDbSource(fail_calls={1, 2, 3})

        with pytest.raises(TMDbError) as excinfo:
            enrich_tmdb_keywords(conn, source)

    message = str(excinfo.value)
    assert "3 consecutive failures" in message
    assert "3 title(s) processed, 3 failed" in message
    assert "scripted failure on call 3" in message
    # The sweep stopped at the third failing title — the fourth and fifth
    # were never asked for.
    assert source.calls == [("1", "movie"), ("2", "movie"), ("3", "movie")]


def test_spelling_variants_from_two_sources_collapse_and_every_surface_is_recorded(
    tmp_path: Path,
) -> None:
    """Acceptance: `Heists`, `heist`, `bank-heist` and `bank heist`, written by
    two different sources, collapse to two stored values (`heist` and
    `bank heist`) — and every raw spelling still appears in `keyword_forms`."""
    from plexdb.keywords import upsert_keyword_form

    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=MOVIE_ID, item_type="movie", title="The Dark Knight", tmdb_id="155")
        source = FakeTMDbSource(keywords_by_id={("155", "movie"): ["Heists", "bank-heist"]})
        enrich_tmdb_keywords(conn, source)

        # A second source stores its own spelling of the same two ideas,
        # through the same normalization every keyword writer shares.
        for surface in ("heist", "bank heist"):
            stored = upsert_keyword_form(conn, surface)
            conn.execute(
                "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
                "VALUES (?, 'keywords', 'mdblist', 'keyword', ?, '2020-01-01T00:00:00+00:00')",
                (MOVIE_ID, stored),
            )
        conn.commit()

        values = {
            r["value"]
            for r in _rows(
                conn,
                "SELECT value FROM enrichment WHERE item_id = ? AND namespace = 'keywords'",
                (MOVIE_ID,),
            )
        }
        form_rows = _rows(conn, "SELECT surface, keyword FROM keyword_forms")
        forms = {r["surface"]: r["keyword"] for r in form_rows}

    assert values == {"heist", "bank heist"}
    assert forms["Heists"] == "heist"
    assert forms["heist"] == "heist"
    assert forms["bank-heist"] == "bank heist"
    assert forms["bank heist"] == "bank heist"


def test_no_bookkeeping_row_ever_lands_in_the_keyword_namespace(tmp_path: Path) -> None:
    """The rule ADR-0013 exists to make unbreakable.

    Before issue #41 the fetch cursor was an `enrichment` row keyed `_fetched`,
    and the only thing keeping it out of the house's taste profile was a
    `NOT LIKE '\\_%'` filter in one query in the reader crate. This asserts the
    property directly — `tmdb_keywords` contains keywords — so a cursor written
    back into it fails here rather than surfacing as a phantom attribute.
    """
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed(conn, item_id=MOVIE_ID, item_type="movie", title="The Dark Knight", tmdb_id="155")
        source = FakeTMDbSource(keywords_by_id={("155", "movie"): ["superhero"]})

        enrich_tmdb_keywords(conn, source)

        keys = {
            row["key"]
            for row in conn.execute(
                "SELECT DISTINCT key FROM enrichment WHERE namespace = ?", (NAMESPACE,)
            )
        }
        cursor_rows = conn.execute("SELECT namespace, key FROM enrichment_cursor").fetchall()

    assert keys == {"keyword"}
    assert [tuple(row) for row in cursor_rows] == [(NAMESPACE, "fetched")]
