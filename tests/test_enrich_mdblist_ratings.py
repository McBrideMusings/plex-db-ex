"""The MDBList ratings sweep against the batch responses recorded live in
`fixtures/mdblist/batch_imdb_*.json` — never the live service."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from mdblist_fixtures import RecordedRatingsSource

from plexdb import enrich_mdblist_ratings as ratings_module
from plexdb.commands import enrich_mdblist_ratings as enrich_mdblist_ratings_cmd
from plexdb.enrich_mdblist_ratings import enrich_mdblist_ratings, wipe
from plexdb.errors import MDBListError
from plexdb.store import init as init_store
from plexdb.store import open_store

CHIEF = "imdb:tt21301418"
MISHIMA = "imdb:tt0089603"
UNKNOWN = "imdb:tt9999999999"  # left out of the recorded answer, as MDBList left it out
SOPRANOS = "imdb:tt0141842"
NO_IMDB = "tmdb:550"
AN_EPISODE = "imdb:tt0959621"


def _seed(c: sqlite3.Connection) -> None:
    for item_id, kind, ids in (
        (CHIEF, "movie", [("imdb", "tt21301418")]),
        (MISHIMA, "movie", [("imdb", "tt0089603"), ("tmdb", "27064")]),
        (UNKNOWN, "movie", [("imdb", "tt9999999999")]),
        (SOPRANOS, "show", [("imdb", "tt0141842")]),
        (NO_IMDB, "movie", [("tmdb", "550")]),
        (AN_EPISODE, "episode", [("imdb", "tt0959621")]),
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


@pytest.fixture
def one_per_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """One title per request, so a cap of N requests is a cap of N titles."""
    monkeypatch.setattr(ratings_module, "BATCH_SIZE", 1)


def _rows(conn: sqlite3.Connection, item_id: str) -> dict[str, str]:
    """key → value for one title's ratings/mdblist rows."""
    return {
        row["key"]: row["value"]
        for row in conn.execute(
            "SELECT key, value FROM enrichment "
            "WHERE item_id = ? AND namespace = 'ratings' AND source = 'mdblist'",
            (item_id,),
        )
    }


def _cursors(conn: sqlite3.Connection, key: str = "fetched") -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT item_id FROM enrichment_cursor "
            "WHERE namespace = 'ratings' AND source = 'mdblist' AND key = ?",
            (key,),
        )
    }


def _asked(source: RecordedRatingsSource) -> list[str]:
    return [imdb_id for _, ids in source.calls for imdb_id in ids]


def test_the_recording_writes_each_non_null_rating_and_its_votes_verbatim(conn: Any) -> None:
    stats = enrich_mdblist_ratings(conn, RecordedRatingsSource())

    assert _rows(conn, MISHIMA) == {
        "imdb": "7.9",
        "imdb_votes": "17303",
        "metacritic": "84",
        "metacritic_votes": "15",
        "metacriticuser": "8.0",
        "metacriticuser_votes": "33",
        "trakt": "78",
        "trakt_votes": "554",
        "tomatoes": "79",
        "tomatoes_votes": "73",
        "popcorn": "90",
        "popcorn_votes": "267",
        "tmdb": "78",
        "tmdb_votes": "372",
        "letterboxd": "8.8",
        "letterboxd_votes": "118935",
        # MDBList's own 0–100 score is null here; the site's value is not.
        "rogerebert": "4.0",
    }
    ranks = conn.execute(
        "SELECT COUNT(*), SUM(rank IS NULL) FROM enrichment WHERE namespace = 'ratings'"
    ).fetchone()
    assert ranks[0] == ranks[1] > 0
    assert stats.titles_rated == 3


def test_a_null_value_writes_no_row_even_with_votes(conn: Any) -> None:
    enrich_mdblist_ratings(conn, RecordedRatingsSource())

    chief = _rows(conn, CHIEF)
    # Recorded as {"source": "metacritic", "value": null, "votes": 1}.
    assert "metacritic" not in chief and "metacritic_votes" not in chief
    assert "myanimelist" not in chief and "rogerebert" not in chief
    assert chief["imdb"] == "4.8"
    sopranos = _rows(conn, SOPRANOS)
    assert sopranos["popcorn"] == "96" and "popcorn_votes" not in sopranos


def test_a_title_mdblist_does_not_know_gets_a_cursor_and_no_rows(conn: Any) -> None:
    stats = enrich_mdblist_ratings(conn, RecordedRatingsSource())

    assert _rows(conn, UNKNOWN) == {}
    assert UNKNOWN in _cursors(conn)
    assert stats.titles_not_found == 1 and stats.titles_fetched == 4


def test_movies_and_shows_go_in_separate_batches_and_only_with_an_imdb_id(conn: Any) -> None:
    source = RecordedRatingsSource()

    stats = enrich_mdblist_ratings(conn, source)

    assert source.calls == [
        ("movie", ["tt0089603", "tt21301418", "tt9999999999"]),
        ("show", ["tt0141842"]),
    ]
    assert stats.titles_seen == 5 and stats.titles_skipped_no_imdb_id == 1
    assert stats.requests_sent == 2


def test_a_second_run_inside_the_window_sends_no_request(conn: Any) -> None:
    enrich_mdblist_ratings(conn, RecordedRatingsSource())
    again = RecordedRatingsSource()

    stats = enrich_mdblist_ratings(conn, again, stale_days=45)

    assert again.calls == []
    assert stats.titles_cached == 4


@pytest.mark.usefixtures("one_per_batch")
def test_the_request_cap_stops_the_run_and_the_next_run_continues(conn: Any) -> None:
    first = RecordedRatingsSource()

    stats = enrich_mdblist_ratings(conn, first, limit=2)

    assert _asked(first) == ["tt0089603", "tt0141842"]  # item_id order
    assert stats.requests_sent == 2 and stats.titles_capped == 2
    second = RecordedRatingsSource()
    enrich_mdblist_ratings(conn, second, limit=2)
    assert _asked(second) == ["tt21301418", "tt9999999999"]
    third = RecordedRatingsSource()
    enrich_mdblist_ratings(conn, third, limit=2)
    assert third.calls == []


def test_a_failed_request_writes_nothing_for_its_batch(conn: Any) -> None:
    stats = enrich_mdblist_ratings(conn, RecordedRatingsSource(fail_calls={1}))

    assert stats.requests_failed == 1 and stats.titles_failed == 3
    assert _rows(conn, MISHIMA) == {}
    assert _cursors(conn) == {SOPRANOS}
    assert _cursors(conn, "attempted") == {CHIEF, MISHIMA, UNKNOWN}
    retry = RecordedRatingsSource()
    enrich_mdblist_ratings(conn, retry)
    assert retry.calls == [("movie", ["tt0089603", "tt21301418", "tt9999999999"])]
    assert _cursors(conn, "attempted") == set()  # the successful fetch clears it


@pytest.mark.usefixtures("one_per_batch")
def test_a_failed_batch_goes_behind_the_untried_ones_under_the_cap(conn: Any) -> None:
    enrich_mdblist_ratings(conn, RecordedRatingsSource(fail_calls={1}), limit=1)

    assert _cursors(conn, "attempted") == {MISHIMA}
    second = RecordedRatingsSource()
    enrich_mdblist_ratings(conn, second, limit=3)
    assert _asked(second) == ["tt0141842", "tt21301418", "tt9999999999"]


@pytest.mark.usefixtures("one_per_batch")
def test_three_failed_requests_in_a_row_abort(conn: Any) -> None:
    with pytest.raises(MDBListError, match="consecutive"):
        enrich_mdblist_ratings(conn, RecordedRatingsSource(fail_calls={1, 2, 3}))


def test_rewipe_removes_only_mdblist_ratings(conn: Any) -> None:
    enrich_mdblist_ratings(conn, RecordedRatingsSource())
    conn.execute(
        "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
        "VALUES (?, 'keywords', 'tmdb', 'keyword', 'dream', '2026-01-01T00:00:00+00:00')",
        (MISHIMA,),
    )
    conn.execute(
        "INSERT INTO collection (collection_id, source, name, observed_at) "
        "VALUES ('mdblist:2194', 'mdblist', 'Latest', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO collection_membership (collection_id, item_id, rank, observed_at) "
        "VALUES ('mdblist:2194', ?, 1, '2026-01-01T00:00:00+00:00')",
        (MISHIMA,),
    )
    conn.commit()

    assert wipe(conn) > 0

    remaining = conn.execute("SELECT DISTINCT namespace, source FROM enrichment").fetchall()
    assert [tuple(row) for row in remaining] == [("keywords", "tmdb")]
    assert conn.execute("SELECT COUNT(*) FROM enrichment_cursor").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM collection_membership").fetchone()[0] == 1


def test_the_limit_variable_reaches_the_run_and_the_report_prints(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ratings_module, "BATCH_SIZE", 1)
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("MDBLIST_API_KEY", "test-key")
    monkeypatch.setenv("MDBLIST_RATINGS_MAX_REQUESTS", "1")
    source = RecordedRatingsSource()
    monkeypatch.setattr(enrich_mdblist_ratings_cmd, "LiveMDBListClient", lambda key: source)
    from plexdb.cli import build_parser

    args = build_parser().parse_args(["enrich-mdblist-ratings"])

    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert len(source.calls) == 1
    assert "3 left for the next run" in out
    assert "1 request(s) sent, 0 failed; 9 rating(s) and 8 vote count(s) written" in out
