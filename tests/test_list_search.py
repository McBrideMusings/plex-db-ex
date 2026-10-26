"""The outward search: watched titles as seeds, a fake MDBList list search, and the rules
the bead states — a list surfaced for enough seeds is fetched with provenance `search`, one
surfaced for fewer is not, an excluded list is skipped, and a seed is not asked twice inside
its staleness window."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fixture_store import init as init_store
from mdblist_fixtures import FakeMDBListSource, a_list, an_entry

from plexdb.errors import MDBListQuotaError
from plexdb.list_search import refresh_list_search, wipe_searched
from plexdb.store import open_store

ALIEN = "imdb:tt0078748"
ALIENS = "imdb:tt0090605"
HEIST = "imdb:tt0000001"
UNWATCHED = "imdb:tt0000002"

XENOMORPH = a_list(11, name="Alien Saga", slug="alien-saga", user_name="lina", size=3)
SOLO = a_list(12, name="Heist Night", slug="heist-night", user_name="omar", size=1)
NOISE = a_list(13, name="Aliens Everywhere", slug="aliens-everywhere", user_name="noor", size=2)
XENOMORPH_URL = "https://mdblist.com/lists/lina/alien-saga"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    init_store(tmp_path / "s.db")
    with open_store(tmp_path / "s.db") as opened:
        yield opened


def _title(conn: sqlite3.Connection, item_id: str, title: str, *, played: bool) -> None:
    conn.execute(
        "INSERT INTO items (item_id, type, title) VALUES (?, 'movie', ?)", (item_id, title)
    )
    conn.execute(
        "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
        "VALUES (?, 'imdb', ?, 'movie', '2026-01-01T00:00:00+00:00')",
        (item_id, item_id.removeprefix("imdb:")),
    )
    if played:
        conn.execute(
            "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) "
            "VALUES (?, ?, 1, 1700000000)",
            (f"h-{item_id}", item_id),
        )
    conn.commit()


def _library(conn: sqlite3.Connection) -> None:
    _title(conn, ALIEN, "Alien", played=True)
    _title(conn, ALIENS, "Aliens", played=True)
    _title(conn, HEIST, "Heat", played=True)
    _title(conn, UNWATCHED, "Unwatched", played=False)


def _source() -> FakeMDBListSource:
    return FakeMDBListSource(
        # "Alien Saga" holds two library titles; "Heist Night" one; "Aliens Everywhere" none.
        search_results={
            "Alien": [XENOMORPH],
            "Aliens": [XENOMORPH, NOISE],
            "Heat": [SOLO],
        },
        named={
            ("lina", "alien-saga"): XENOMORPH,
            ("omar", "heist-night"): SOLO,
            ("noor", "aliens-everywhere"): NOISE,
        },
        entries_by_list={
            11: [an_entry(imdb="tt0078748"), an_entry(imdb="tt0090605"), an_entry(imdb="tt9")],
            12: [an_entry(imdb="tt0000001")],
        },
    )


def _decide(conn: sqlite3.Connection, url: str, decision: str) -> None:
    conn.execute(
        "INSERT INTO list_decision (url, decision, decided_at, added) VALUES (?, ?, ?, 0)",
        (url, decision, datetime.now(UTC).isoformat(timespec="seconds")),
    )
    conn.commit()


def test_a_candidate_holding_enough_library_titles_is_kept_with_provenance_search(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    _library(conn)
    source = _source()

    stats = refresh_list_search(conn, source, min_titles=2)

    assert sorted(source.search_calls) == ["Alien", "Aliens", "Heat"]
    assert sorted(source.entry_calls) == [11, 12, 13]
    rows = [
        tuple(r)
        for r in conn.execute("SELECT collection_id, provenance, seed_count FROM collection")
    ]
    assert rows == [("mdblist:11", "search", 2)]
    assert conn.execute("SELECT COUNT(*) FROM collection_membership").fetchone()[0] == 2
    assert (stats.seeds_asked, stats.lists_found, stats.lists_qualified, stats.lists_rejected) == (
        3,
        3,
        1,
        2,
    )
    out = capsys.readouterr().out
    assert 'search "Alien" (seed imdb:tt0078748) -> 1 list(s)' in out
    assert f"{XENOMORPH_URL} -> HTTP 200, 3 entries parsed, 2 resolved to the library" in out


def test_an_excluded_list_is_not_fetched_even_when_it_would_qualify(
    conn: sqlite3.Connection,
) -> None:
    _library(conn)
    _decide(conn, XENOMORPH_URL, "excluded")
    source = _source()

    stats = refresh_list_search(conn, source, min_titles=2)

    assert 11 not in source.entry_calls
    assert conn.execute("SELECT COUNT(*) FROM collection").fetchone()[0] == 0
    assert stats.lists_qualified == 0


def test_a_second_run_inside_the_window_asks_no_seed_and_fetches_no_list_again(
    conn: sqlite3.Connection,
) -> None:
    _library(conn)
    refresh_list_search(conn, _source(), min_titles=2)

    second = _source()
    stats = refresh_list_search(conn, second, min_titles=2)

    assert second.search_calls == []
    assert second.entry_calls == []  # the kept list is cached, the rejected ones remembered
    assert (stats.seeds_asked, stats.seeds_cached) == (0, 3)


def test_a_run_asks_at_most_the_seed_cap_and_fetches_at_most_the_list_cap(
    conn: sqlite3.Connection,
) -> None:
    _library(conn)
    first = _source()
    refresh_list_search(conn, first, limit=1, max_lists=1, min_titles=2)
    assert len(first.search_calls) == 1
    assert len(first.entry_calls) == 1

    second = _source()
    refresh_list_search(conn, second, limit=2, max_lists=10, min_titles=2)
    assert len(second.search_calls) == 2  # the one seed asked already is not asked again
    assert sorted(first.entry_calls + second.entry_calls) == [11, 12, 13]


def test_the_quota_ending_stops_the_run_and_keeps_the_seeds_already_asked(
    conn: sqlite3.Connection,
) -> None:
    _library(conn)
    source = _source()
    source.failing_searches["Aliens"] = MDBListQuotaError("quota spent", 429)

    with pytest.raises(MDBListQuotaError):
        refresh_list_search(conn, source, min_titles=2)

    asked = conn.execute(
        "SELECT COUNT(*) FROM enrichment_cursor WHERE namespace = 'list_search' AND key = 'fetched'"
    ).fetchone()[0]
    assert asked >= 1
    assert "Aliens" in source.search_calls


def test_wiping_the_search_forgets_searched_lists_and_leaves_feed_lists(
    conn: sqlite3.Connection,
) -> None:
    _library(conn)
    refresh_list_search(conn, _source(), min_titles=2)
    conn.execute(
        "INSERT INTO collection (collection_id, source, name, observed_at, provenance) "
        "VALUES ('mdblist:99', 'mdblist', 'Top', '2026-01-01T00:00:00+00:00', 'feed')"
    )
    conn.commit()

    assert wipe_searched(conn)[0] == 1

    assert [r[0] for r in conn.execute("SELECT collection_id FROM collection")] == ["mdblist:99"]
    assert conn.execute("SELECT COUNT(*) FROM enrichment_cursor").fetchone()[0] == 0


def test_a_rejected_candidate_is_not_stored_and_a_person_added_list_is_never_dropped(
    conn: sqlite3.Connection,
) -> None:
    _library(conn)
    conn.execute(
        "INSERT INTO collection (collection_id, source, name, url, observed_at, provenance) "
        "VALUES ('mdblist:12', 'mdblist', 'Heist Night', "
        "'https://mdblist.com/lists/omar/heist-night', '2000-01-01T00:00:00+00:00', 'added')"
    )
    conn.commit()
    source = _source()

    refresh_list_search(conn, source, min_titles=2)

    rows = {r[0]: r[1] for r in conn.execute("SELECT collection_id, provenance FROM collection")}
    assert rows == {"mdblist:11": "search", "mdblist:12": "added"}
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM enrichment_cursor WHERE key LIKE 'rejected:%'"
        ).fetchone()[0]
        == 2
    )  # the added list and the empty one are remembered as rejected
    again = _source()
    refresh_list_search(conn, again, min_titles=2)
    assert again.entry_calls == []
