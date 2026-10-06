"""The Letterboxd sweep against the recorded pages in `fixtures/letterboxd/` —
never the live site."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from letterboxd_fixtures import INCEPTION_TMDB, RecordedLetterboxdSource, load, markerless

from plexdb.commands import enrich_letterboxd as enrich_letterboxd_cmd
from plexdb.enrich_letterboxd import enrich_letterboxd, wipe
from plexdb.errors import LetterboxdError
from plexdb.store import init as init_store
from plexdb.store import open_store

INCEPTION = "imdb:tt1375666"
MIRROR = "imdb:tt0072443"  # tmdb 1396; not in the recorded source, so not listed
NO_TMDB = "plex:12345"
A_SHOW = "imdb:tt0903747"


def _seed(c: sqlite3.Connection) -> None:
    for item_id, kind, ids in (
        (INCEPTION, "movie", [("imdb", "tt1375666"), ("tmdb", INCEPTION_TMDB)]),
        (MIRROR, "movie", [("imdb", "tt0072443"), ("tmdb", "1396")]),
        (NO_TMDB, "movie", []),
        (A_SHOW, "show", [("tmdb", "1396")]),
    ):
        c.execute(
            "INSERT INTO items (item_id, type, title) VALUES (?, ?, ?)", (item_id, kind, item_id)
        )
        for ns, value in ids:
            c.execute(
                "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
                "VALUES (?, ?, ?, ?, '2026-01-01T00:00:00+00:00')",
                (item_id, ns, value, kind),
            )
    c.commit()


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "plexdb.db"
    init_store(path)
    with open_store(path) as c:
        _seed(c)
    return path


@pytest.fixture
def conn(store: Path) -> Any:
    with open_store(store) as c:
        yield c


def _rows(conn: sqlite3.Connection, item_id: str) -> dict[str, tuple[str, int | None]]:
    """value → (key, rank) for one title's letterboxd rows."""
    return {
        row["value"]: (row["key"], row["rank"])
        for row in conn.execute(
            "SELECT key, value, rank FROM enrichment "
            "WHERE item_id = ? AND namespace = 'keywords' AND source = 'letterboxd'",
            (item_id,),
        )
    }


def _cursors(conn: sqlite3.Connection, key: str = "fetched") -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT item_id FROM enrichment_cursor WHERE source = 'letterboxd' AND key = ?", (key,)
        )
    }


def test_inception_stores_its_themes_unranked(conn: Any) -> None:
    stats = enrich_letterboxd(conn, RecordedLetterboxdSource())

    rows = _rows(conn, INCEPTION)
    assert len(rows) == 7
    assert rows["high speed and special op"] == ("keyword", None)
    assert rows["dreamlik quirki and surreal storytel"] == ("keyword", None)
    surfaces = dict(conn.execute("SELECT surface, keyword FROM keyword_forms").fetchall())
    assert surfaces["High speed and special ops"] == "high speed and special op"
    assert conn.execute("SELECT COUNT(*) FROM keyword_roles").fetchone()[0] == 0
    assert stats.titles_matched == 1 and stats.keywords_written == 7


def test_only_movies_with_a_tmdb_id_are_asked(conn: Any) -> None:
    source = RecordedLetterboxdSource()

    stats = enrich_letterboxd(conn, source)

    assert sorted(source.calls) == ["1396", INCEPTION_TMDB]
    assert stats.titles_seen == 3 and stats.titles_skipped_no_tmdb_id == 1


def test_a_film_not_listed_gets_a_cursor_and_no_keywords(conn: Any) -> None:
    stats = enrich_letterboxd(conn, RecordedLetterboxdSource())

    assert _rows(conn, MIRROR) == {}
    assert MIRROR in _cursors(conn)
    assert stats.titles_not_listed == 1 and stats.titles_fetched == 2


def test_a_page_without_the_film_marker_is_a_parse_failure_with_no_cursor(conn: Any) -> None:
    source = RecordedLetterboxdSource(
        pages={INCEPTION_TMDB: markerless(load("film_inception.html"))}
    )

    stats = enrich_letterboxd(conn, source)

    assert stats.parse_failures == 1 and stats.pages_fetched == 1
    assert _rows(conn, INCEPTION) == {}
    assert INCEPTION not in _cursors(conn)
    again = RecordedLetterboxdSource()
    enrich_letterboxd(conn, again)
    assert again.calls == [INCEPTION_TMDB]


def test_a_second_run_inside_the_window_sends_no_request(conn: Any) -> None:
    enrich_letterboxd(conn, RecordedLetterboxdSource())
    again = RecordedLetterboxdSource()

    stats = enrich_letterboxd(conn, again, stale_days=45)

    assert again.calls == []
    assert stats.titles_cached == 2


def test_the_cap_stops_the_run_and_the_next_run_continues(conn: Any) -> None:
    first = RecordedLetterboxdSource()

    stats = enrich_letterboxd(conn, first, limit=1)

    assert first.calls == ["1396"]  # item_id order: imdb:tt0072443 before imdb:tt1375666
    assert stats.titles_fetched == 1 and stats.titles_capped == 1
    second = RecordedLetterboxdSource()
    enrich_letterboxd(conn, second, limit=1)
    assert second.calls == [INCEPTION_TMDB]
    third = RecordedLetterboxdSource()
    enrich_letterboxd(conn, third, limit=1)
    assert third.calls == []


def test_a_failed_request_writes_nothing_and_is_asked_again(conn: Any) -> None:
    stats = enrich_letterboxd(conn, RecordedLetterboxdSource(fail_calls={1}))

    assert stats.titles_failed == 1 and stats.titles_fetched == 1
    retry = RecordedLetterboxdSource()
    enrich_letterboxd(conn, retry)
    assert len(retry.calls) == 1
    assert _cursors(conn, "attempted") == set()  # the successful fetch clears it


def test_a_title_that_failed_goes_behind_the_untried_ones_under_the_cap(conn: Any) -> None:
    # MIRROR sorts first by item_id; its failure must not hold the one-title cap.
    stats = enrich_letterboxd(conn, RecordedLetterboxdSource(fail_calls={1}), limit=1)

    assert stats.titles_failed == 1
    assert _cursors(conn, "attempted") == {MIRROR} and _cursors(conn) == set()
    second = RecordedLetterboxdSource()
    enrich_letterboxd(conn, second, limit=1)
    assert second.calls == [INCEPTION_TMDB]
    third = RecordedLetterboxdSource()
    enrich_letterboxd(conn, third, limit=1)
    assert third.calls == ["1396"]


def test_a_parse_failure_goes_behind_the_untried_ones_under_the_cap(conn: Any) -> None:
    broken = {"1396": markerless(load("film_inception.html"))}

    enrich_letterboxd(conn, RecordedLetterboxdSource(pages=broken), limit=1)
    second = RecordedLetterboxdSource(pages=broken)
    enrich_letterboxd(conn, second, limit=1)

    assert second.calls == [INCEPTION_TMDB]


def test_three_failed_requests_in_a_row_abort(conn: Any) -> None:
    conn.execute("INSERT INTO items (item_id, type, title) VALUES ('tmdb:550', 'movie', 'x')")
    conn.execute(
        "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
        "VALUES ('tmdb:550', 'tmdb', '550', 'movie', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()

    with pytest.raises(LetterboxdError, match="consecutive"):
        enrich_letterboxd(conn, RecordedLetterboxdSource(fail_calls={1, 2, 3}))


def test_rewipe_removes_only_letterboxd_rows(conn: Any) -> None:
    enrich_letterboxd(conn, RecordedLetterboxdSource())
    conn.execute(
        "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
        "VALUES (?, 'keywords', 'tmdb', 'keyword', 'dream', '2026-01-01T00:00:00+00:00')",
        (INCEPTION,),
    )
    conn.execute(
        "INSERT INTO enrichment_cursor (item_id, namespace, source, key, fetched_at) "
        "VALUES (?, 'keywords', 'tmdb', 'fetched', '2026-01-01T00:00:00+00:00')",
        (INCEPTION,),
    )
    conn.commit()

    assert wipe(conn) > 0

    assert [r[0] for r in conn.execute("SELECT DISTINCT source FROM enrichment")] == ["tmdb"]
    assert [r[0] for r in conn.execute("SELECT DISTINCT source FROM enrichment_cursor")] == ["tmdb"]


def _run_command(
    store: Path, monkeypatch: pytest.MonkeyPatch, source: RecordedLetterboxdSource, *flags: str
) -> int:
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("LETTERBOXD_MAX_TITLES", "")
    monkeypatch.setattr(enrich_letterboxd_cmd, "LiveLetterboxdClient", lambda: source)
    from plexdb.cli import build_parser

    args = build_parser().parse_args(["enrich-letterboxd", *flags])
    return int(args.func(args))


def test_parse_failures_past_the_share_exit_non_zero_and_print_the_count(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = RecordedLetterboxdSource(
        pages={
            INCEPTION_TMDB: markerless(load("film_inception.html")),
            "1396": load("film_inception.html").replace(INCEPTION_TMDB, "1396"),
        }
    )

    code = _run_command(store, monkeypatch, source)

    out = capsys.readouterr().out
    assert code == 1
    assert "1 of 2 film page(s) had no film marker, over the 10% limit" in out


def test_a_clean_run_exits_zero(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run_command(store, monkeypatch, RecordedLetterboxdSource())

    assert code == 0
    assert "0 parse failure(s) of 1 film page(s)" in capsys.readouterr().out


def test_the_limit_flag_reaches_the_run(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = RecordedLetterboxdSource()

    _run_command(store, monkeypatch, source, "--limit", "1")

    assert len(source.calls) == 1
    assert "1 left for the next run" in capsys.readouterr().out
