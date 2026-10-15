"""Harvesting crowd lists into `collection` and `collection_membership`.

Drives `refresh_mdblist` against the in-memory `MDBListSource` fake in
`mdblist_fixtures.py` — never a live service. The rules under test are the ones
issue #34 and ADR-0012 are about: rank is array position, a list's own facts
are stored once, nothing is computed, an entry outside the library is dropped
rather than invented, and a re-pull replaces rather than appends.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from mdblist_fixtures import FakeMDBListSource, a_list, an_entry

from plexdb.collections import MDBLIST_SOURCE, refresh_mdblist, wipe_source
from plexdb.errors import MDBListError, MDBListQuotaError
from plexdb.store import init as init_store
from plexdb.store import open_store

DARK_KNIGHT = "imdb:tt0468569"
BATMAN_BEGINS = "imdb:tt0372784"
BREAKING_BAD = "imdb:tt0903747"


def _open(store: Path) -> Any:
    init_store(store)
    return open_store(store)


def _seed(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    item_type: str,
    title: str,
    **ids: str,
) -> None:
    conn.execute(
        "INSERT INTO items (item_id, type, title) VALUES (?, ?, ?)",
        (item_id, item_type, title),
    )
    for ns, value in ids.items():
        conn.execute(
            "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
            "VALUES (?, ?, ?, ?, '2026-01-01T00:00:00+00:00')",
            (item_id, ns, value, item_type),
        )
    conn.commit()


def _memberships(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT collection_id, item_id, rank, mentions FROM collection_membership "
            "ORDER BY collection_id, rank"
        )
    ]


def test_a_list_becomes_one_collection_row_and_ranked_memberships(tmp_path: Path) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        _seed(
            conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", imdb="tt0468569"
        )
        _seed(
            conn, item_id=BATMAN_BEGINS, item_type="movie", title="Batman Begins", imdb="tt0372784"
        )
        source = FakeMDBListSource(
            lists=[
                a_list(
                    14, name="Top Watched", slug="top-watched", user_name="lina", size=48, likes=961
                )
            ],
            entries_by_list={
                14: [an_entry(imdb="tt0468569"), an_entry(imdb="tt0372784")],
            },
        )

        stats = refresh_mdblist(conn, source)

        assert stats.memberships_written == 2
        collection = conn.execute(
            "SELECT collection_id, source, name, url, size, likes FROM collection"
        ).fetchone()
        assert tuple(collection) == (
            "mdblist:14",
            MDBLIST_SOURCE,
            "Top Watched",
            "https://mdblist.com/lists/lina/top-watched",
            48,
            961,
        )
        assert _memberships(conn) == [
            ("mdblist:14", DARK_KNIGHT, 1, None),
            ("mdblist:14", BATMAN_BEGINS, 2, None),
        ]


def test_rank_is_position_in_the_list_not_the_sources_own_rank_field(tmp_path: Path) -> None:
    """MDBList sends a `rank` on each entry that is a different ordering.

    The first entry of a real 300-item list carried `"rank": 333`. Storing that
    as the membership rank would say a title sits 333rd on a list of 300.
    """
    with _open(tmp_path / "plexdb.db") as conn:
        _seed(conn, item_id=BREAKING_BAD, item_type="show", title="Breaking Bad", tvdb="81189")
        source = FakeMDBListSource(
            lists=[a_list(2194, size=300)],
            entries_by_list={2194: [an_entry("show", tvdb="81189")]},
        )

        refresh_mdblist(conn, source)

        assert _memberships(conn) == [("mdblist:2194", BREAKING_BAD, 1, None)]


def test_an_entry_outside_the_library_is_dropped_and_size_still_records_the_whole_list(
    tmp_path: Path,
) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        _seed(
            conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", imdb="tt0468569"
        )
        source = FakeMDBListSource(
            lists=[a_list(14, size=100)],
            entries_by_list={
                14: [an_entry(imdb="tt9999999"), an_entry(imdb="tt0468569")],
            },
        )

        stats = refresh_mdblist(conn, source)

        assert stats.entries_not_in_library == 1
        assert stats.memberships_written == 1
        # Position 2, not 1: rank is the entry's place on the list as the
        # source presented it, not its place among the survivors.
        assert _memberships(conn) == [("mdblist:14", DARK_KNIGHT, 2, None)]
        assert conn.execute("SELECT size FROM collection").fetchone()[0] == 100


def test_an_entry_resolves_through_the_strongest_id_it_carries(tmp_path: Path) -> None:
    """`imdb` beats `tmdb` beats `tvdb` — the same order `derive_item_id` uses,
    so a title resolves here the way it was named at walk time."""
    with _open(tmp_path / "plexdb.db") as conn:
        _seed(conn, item_id=BREAKING_BAD, item_type="show", title="Breaking Bad", tvdb="81189")
        source = FakeMDBListSource(
            lists=[a_list(2194)],
            entries_by_list={2194: [an_entry("show", imdb="tt0903747", tvdb="81189")]},
        )

        refresh_mdblist(conn, source)

        assert _memberships(conn) == [("mdblist:2194", BREAKING_BAD, 1, None)]


def test_a_movie_and_a_show_sharing_an_id_number_are_not_confused(tmp_path: Path) -> None:
    """TMDB numbers movies and shows in two lists that both start at 1 (schema
    v5). Resolving without the media type hands a show the movie's identity."""
    with _open(tmp_path / "plexdb.db") as conn:
        _seed(conn, item_id="tmdb:1678", item_type="movie", title="Godzilla", tmdb="1678")
        _seed(conn, item_id="tvdb:71292", item_type="show", title="The Golden Girls", tmdb="1678")
        source = FakeMDBListSource(
            lists=[a_list(14)],
            entries_by_list={14: [an_entry("show", tmdb="1678")]},
        )

        refresh_mdblist(conn, source)

        assert _memberships(conn) == [("mdblist:14", "tvdb:71292", 1, None)]


def test_a_title_listed_twice_keeps_its_first_position(tmp_path: Path) -> None:
    """Two ids on one list can land on the same `item_id`. Inserting both would
    crash the harvest on the table's primary key."""
    with _open(tmp_path / "plexdb.db") as conn:
        _seed(
            conn,
            item_id=DARK_KNIGHT,
            item_type="movie",
            title="The Dark Knight",
            imdb="tt0468569",
            tmdb="155",
        )
        source = FakeMDBListSource(
            lists=[a_list(14)],
            entries_by_list={14: [an_entry(imdb="tt0468569"), an_entry(tmdb="155")]},
        )

        stats = refresh_mdblist(conn, source)

        assert stats.entries_duplicate == 1
        assert _memberships(conn) == [("mdblist:14", DARK_KNIGHT, 1, None)]


def test_a_re_pull_replaces_the_list_rather_than_appending(tmp_path: Path) -> None:
    """MDBList marks lists `dynamic` — the source regenerates them. Appending
    would accumulate every title that ever passed through."""
    with _open(tmp_path / "plexdb.db") as conn:
        _seed(
            conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", imdb="tt0468569"
        )
        _seed(
            conn, item_id=BATMAN_BEGINS, item_type="movie", title="Batman Begins", imdb="tt0372784"
        )
        source = FakeMDBListSource(
            lists=[a_list(14)],
            entries_by_list={14: [an_entry(imdb="tt0468569"), an_entry(imdb="tt0372784")]},
        )
        refresh_mdblist(conn, source)

        source.entries_by_list[14] = [an_entry(imdb="tt0372784")]
        refresh_mdblist(conn, source, stale_days=0)

        assert _memberships(conn) == [("mdblist:14", BATMAN_BEGINS, 1, None)]


def test_a_fresh_list_is_not_re_fetched(tmp_path: Path) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        _seed(
            conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", imdb="tt0468569"
        )
        source = FakeMDBListSource(
            lists=[a_list(14)],
            entries_by_list={14: [an_entry(imdb="tt0468569")]},
        )
        refresh_mdblist(conn, source)

        stats = refresh_mdblist(conn, source)

        assert source.entry_calls == [14]
        assert stats.lists_cached == 1
        assert stats.lists_fetched == 0


def test_a_list_whose_entries_all_fall_outside_the_library_is_still_cached(tmp_path: Path) -> None:
    """The cursor is the `collection` row, not a membership row.

    A list overlapping the library by nothing leaves no membership behind, so a
    cursor read off memberships would re-fetch it on every single run.
    """
    with _open(tmp_path / "plexdb.db") as conn:
        source = FakeMDBListSource(
            lists=[a_list(14)],
            entries_by_list={14: [an_entry(imdb="tt9999999")]},
        )
        refresh_mdblist(conn, source)

        stats = refresh_mdblist(conn, source)

        assert source.entry_calls == [14]
        assert stats.lists_cached == 1


def test_wiping_one_source_leaves_another_untouched(tmp_path: Path) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        _seed(
            conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", imdb="tt0468569"
        )
        source = FakeMDBListSource(
            lists=[a_list(14)],
            entries_by_list={14: [an_entry(imdb="tt0468569")]},
        )
        refresh_mdblist(conn, source)
        conn.execute(
            "INSERT INTO collection (collection_id, source, name, observed_at) "
            "VALUES ('letterboxd:x', 'letterboxd', 'Someone Else', '2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO collection_membership (collection_id, item_id, rank, observed_at) "
            "VALUES ('letterboxd:x', ?, 1, '2026-01-01T00:00:00+00:00')",
            (DARK_KNIGHT,),
        )
        conn.commit()

        conn.execute(
            "INSERT INTO collection_entry (collection_id, rank, ns, value, kind) "
            "VALUES ('letterboxd:x', 1, 'imdb', 'tt0468569', 'movie')"
        )
        conn.commit()

        memberships, entries, collections = wipe_source(conn, MDBLIST_SOURCE)

        assert (memberships, entries, collections) == (1, 1, 1)
        assert conn.execute("SELECT COUNT(*) FROM collection_entry").fetchone()[0] == 1
        assert _memberships(conn) == [("letterboxd:x", DARK_KNIGHT, 1, None)]


def test_no_weight_column_exists(tmp_path: Path) -> None:
    """ADR-0012. A stored weight would freeze one formula into every row and
    could not be inverted back into the facts behind it."""
    with _open(tmp_path / "plexdb.db") as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(collection_membership)").fetchall()
        }

        assert "weight" not in columns
        assert {"rank", "mentions"} <= columns


def _entries(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT collection_id, rank, ns, value, kind, title, year FROM collection_entry "
            "ORDER BY collection_id, rank"
        )
    ]


def test_every_entry_is_stored_and_only_owned_ones_become_memberships(tmp_path: Path) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        _seed(
            conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", imdb="tt0468569"
        )
        source = FakeMDBListSource(
            lists=[a_list(14, size=3)],
            entries_by_list={
                14: [
                    an_entry(imdb="tt9999999", title="Unowned Film", year=1999),
                    an_entry(imdb="tt0468569", title="The Dark Knight", year=2008),
                    an_entry("show", tvdb="81189", title="Breaking Bad", year=2008),
                ],
            },
        )

        stats = refresh_mdblist(conn, source)

        assert _entries(conn) == [
            ("mdblist:14", 1, "imdb", "tt9999999", "movie", "Unowned Film", 1999),
            ("mdblist:14", 2, "imdb", "tt0468569", "movie", "The Dark Knight", 2008),
            ("mdblist:14", 3, "tvdb", "81189", "show", "Breaking Bad", 2008),
        ]
        assert _memberships(conn) == [("mdblist:14", DARK_KNIGHT, 2, None)]
        assert stats.entries_stored == 3
        assert conn.execute("SELECT provenance, seed_count FROM collection").fetchone()[:] == (
            "feed",
            None,
        )


def test_a_re_pull_that_drops_an_entry_removes_it_from_entries_and_memberships(
    tmp_path: Path,
) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        _seed(
            conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", imdb="tt0468569"
        )
        source = FakeMDBListSource(
            lists=[a_list(14)],
            entries_by_list={14: [an_entry(imdb="tt0468569"), an_entry(imdb="tt9999999")]},
        )
        refresh_mdblist(conn, source)

        source.entries_by_list[14] = [an_entry(imdb="tt9999999")]
        refresh_mdblist(conn, source, stale_days=0)

        assert [row[3] for row in _entries(conn)] == ["tt9999999"]
        assert _memberships(conn) == []


def test_a_fresh_list_stored_without_entries_is_fetched_once_more(tmp_path: Path) -> None:
    """A list harvested before entries were kept has a fresh `collection` row and
    nothing in `collection_entry`; the staleness window alone would leave it so."""
    with _open(tmp_path / "plexdb.db") as conn:
        conn.execute(
            "INSERT INTO collection (collection_id, source, name, observed_at) "
            "VALUES ('mdblist:14', 'mdblist', 'Old', ?)",
            (datetime.now(UTC).isoformat(timespec="seconds"),),
        )
        conn.commit()
        source = FakeMDBListSource(
            lists=[a_list(14)], entries_by_list={14: [an_entry(imdb="tt9999999")]}
        )

        refresh_mdblist(conn, source)
        refresh_mdblist(conn, source)

        assert source.entry_calls == [14]
        assert len(_entries(conn)) == 1


def test_each_list_logs_one_line_with_url_status_counts_and_outcome(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        _seed(
            conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", imdb="tt0468569"
        )
        source = FakeMDBListSource(
            lists=[a_list(14, slug="top-watched", user_name="lina")],
            entries_by_list={14: [an_entry(imdb="tt0468569"), an_entry(imdb="tt9999999")]},
        )

        refresh_mdblist(conn, source)
        refresh_mdblist(conn, source)

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == (
        "mdblist: https://mdblist.com/lists/lina/top-watched -> HTTP 200, "
        "2 entries parsed, 1 resolved to the library, written"
    )
    assert lines[1].startswith("mdblist: https://mdblist.com/lists/lina/top-watched -> cached")


def test_a_failed_fetch_logs_its_status_and_reason_and_is_recorded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = FakeMDBListSource(
        lists=[a_list(14, slug="s", user_name="u"), a_list(15, slug="t", user_name="u")],
        failing={14: MDBListError("MDBList returned 503 for https://api.mdblist.com/x", 503)},
        entries_by_list={15: [an_entry(imdb="tt9999999")]},
    )
    with _open(tmp_path / "plexdb.db") as conn:
        stats = refresh_mdblist(conn, source)
        row = conn.execute("SELECT * FROM list_attempt").fetchone()

    assert (stats.lists_failed, stats.lists_fetched) == (1, 1)
    assert tuple(row)[:2] == ("https://mdblist.com/lists/u/s", 1)
    assert row["reason"] == "MDBList returned 503 for https://api.mdblist.com/x"
    assert capsys.readouterr().out.splitlines()[0] == (
        "mdblist: https://mdblist.com/lists/u/s -> HTTP 503, "
        "failed: MDBList returned 503 for https://api.mdblist.com/x"
    )


# ---- list decisions (plex-db-ex-k1q.5) ----

SLUG_URL = "https://mdblist.com/lists/lina/top-watched"


def _decide(conn: sqlite3.Connection, url: str, decision: str, *, added: int = 0) -> None:
    conn.execute(
        "INSERT INTO list_decision (url, decision, decided_at, added) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (url) DO UPDATE SET decision = excluded.decision, "
        "decided_at = excluded.decided_at, added = excluded.added",
        (url, decision, datetime.now(UTC).isoformat(timespec="seconds"), added),
    )
    conn.commit()


def _lists_stored(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        tuple(r)
        for r in conn.execute("SELECT collection_id, provenance FROM collection ORDER BY 1")
    ]


def _top(list_id: int = 14, **kw: Any) -> FakeMDBListSource:
    return FakeMDBListSource(
        lists=[a_list(list_id, slug="top-watched", user_name="lina")],
        entries_by_list={list_id: [an_entry(imdb="tt9999999")]},
        **kw,
    )


def test_an_excluded_list_is_wiped_and_never_fetched_again(tmp_path: Path) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        refresh_mdblist(conn, _top())
        assert _lists_stored(conn) == [("mdblist:14", "feed")]
        _decide(conn, SLUG_URL, "excluded")
        source = _top()

        stats = refresh_mdblist(conn, source, stale_days=0)

        assert _lists_stored(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM collection_entry").fetchone()[0] == 0
        assert source.entry_calls == []
        assert (stats.lists_excluded, stats.lists_removed) == (1, 1)


def test_an_added_url_is_fetched_with_provenance_added(tmp_path: Path) -> None:
    named = a_list(77, name="Mine", slug="mine", user_name="pierce")
    source = FakeMDBListSource(
        named={("pierce", "mine"): named}, entries_by_list={77: [an_entry(imdb="tt9999999")]}
    )
    with _open(tmp_path / "plexdb.db") as conn:
        _decide(conn, "https://mdblist.com/lists/pierce/mine", "added", added=1)

        refresh_mdblist(conn, source)

        assert _lists_stored(conn) == [("mdblist:77", "added")]
        url = conn.execute("SELECT url FROM collection").fetchone()[0]
        assert url == "https://mdblist.com/lists/pierce/mine"
        assert source.find_calls == [("pierce", "mine")]


def test_an_added_list_that_is_also_in_the_feed_is_fetched_once_as_added(tmp_path: Path) -> None:
    source = _top()
    with _open(tmp_path / "plexdb.db") as conn:
        _decide(conn, SLUG_URL, "added", added=1)

        refresh_mdblist(conn, source)

        assert source.entry_calls == [14]
        assert source.find_calls == []
        assert _lists_stored(conn) == [("mdblist:14", "added")]


def test_refetch_ignores_staleness_once(tmp_path: Path) -> None:
    source = _top()
    with _open(tmp_path / "plexdb.db") as conn:
        refresh_mdblist(conn, source)
        conn.execute("UPDATE collection SET observed_at = '2000-01-01T00:00:00+00:00'")
        _decide(conn, SLUG_URL, "refetch")

        refresh_mdblist(conn, source)
        assert source.entry_calls == [14, 14]

        stats = refresh_mdblist(conn, source)
        assert source.entry_calls == [14, 14]
        assert stats.lists_cached == 1


def test_removing_an_added_list_drops_it_but_not_a_feed_list(tmp_path: Path) -> None:
    named = a_list(77, slug="mine", user_name="pierce")
    source = FakeMDBListSource(
        lists=[a_list(14, slug="top-watched", user_name="lina")],
        named={("pierce", "mine"): named},
        entries_by_list={14: [an_entry(imdb="tt9999999")], 77: [an_entry(imdb="tt9999999")]},
    )
    mine = "https://mdblist.com/lists/pierce/mine"
    with _open(tmp_path / "plexdb.db") as conn:
        _decide(conn, mine, "added", added=1)
        refresh_mdblist(conn, source)
        assert _lists_stored(conn) == [("mdblist:14", "feed"), ("mdblist:77", "added")]

        _decide(conn, mine, "removed")
        _decide(conn, SLUG_URL, "removed")
        refresh_mdblist(conn, source)

        assert _lists_stored(conn) == [("mdblist:14", "feed")]


def test_three_failures_in_a_row_stop_a_list_until_a_retry_decision(tmp_path: Path) -> None:
    failing = {14: MDBListError("MDBList returned 503 for u", 503)}
    source = _top(failing=failing)
    with _open(tmp_path / "plexdb.db") as conn:
        for _ in range(3):
            refresh_mdblist(conn, source)
        assert source.entry_calls == [14, 14, 14]
        assert conn.execute("SELECT attempts FROM list_attempt").fetchone()[0] == 3

        stats = refresh_mdblist(conn, source)
        assert source.entry_calls == [14, 14, 14]
        assert stats.lists_stopped == 1

        conn.execute("UPDATE list_attempt SET attempted_at = '2000-01-01T00:00:00+00:00'")
        _decide(conn, SLUG_URL, "retry")
        source.failing.clear()
        refresh_mdblist(conn, source)

        assert source.entry_calls == [14, 14, 14, 14]
        assert conn.execute("SELECT COUNT(*) FROM list_attempt").fetchone()[0] == 0
        assert _lists_stored(conn) == [("mdblist:14", "feed")]


def test_a_retry_that_fails_again_counts_from_one(tmp_path: Path) -> None:
    source = _top(failing={14: MDBListError("MDBList returned 503 for u", 503)})
    with _open(tmp_path / "plexdb.db") as conn:
        conn.execute(
            "INSERT INTO list_attempt (list_key, attempts, attempted_at, reason) "
            "VALUES (?, 3, '2000-01-01T00:00:00+00:00', 'old')",
            (SLUG_URL,),
        )
        _decide(conn, SLUG_URL, "retry")

        refresh_mdblist(conn, source)

        assert conn.execute("SELECT attempts FROM list_attempt").fetchone()[0] == 1


def test_a_list_url_that_does_not_exist_is_a_recorded_failure(tmp_path: Path) -> None:
    source = FakeMDBListSource()
    with _open(tmp_path / "plexdb.db") as conn:
        _decide(conn, "https://mdblist.com/lists/pierce/gone", "added", added=1)

        stats = refresh_mdblist(conn, source)

        assert stats.lists_failed == 1
        row = conn.execute("SELECT list_key, attempts FROM list_attempt").fetchone()
        assert tuple(row) == ("https://mdblist.com/lists/pierce/gone", 1)


@pytest.mark.parametrize(
    "error",
    [MDBListQuotaError("quota", 429), MDBListError("refused", 403)],
)
def test_a_failure_about_no_one_list_stops_the_run_and_records_nothing(
    tmp_path: Path, error: MDBListError
) -> None:
    source = _top(failing={14: error})
    with _open(tmp_path / "plexdb.db") as conn:
        with pytest.raises(MDBListError):
            refresh_mdblist(conn, source)
        assert conn.execute("SELECT COUNT(*) FROM list_attempt").fetchone()[0] == 0
