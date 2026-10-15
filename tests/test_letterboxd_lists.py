"""Harvesting Letterboxd lists: parsing the recorded list pages, reading them through
`LiveLetterboxdClient` over `httpx.MockTransport`, and `refresh_letterboxd_lists` over a
recorded source — never the live site."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from letterboxd_fixtures import (
    LIST_FILMS,
    LIST_SLUG,
    LIST_URL,
    LIST_USER,
    RecordedListSource,
    film_page,
    load,
)
from mdblist_fixtures import FakeMDBListSource

from plexdb.collections import refresh_mdblist
from plexdb.errors import LetterboxdError
from plexdb.letterboxd_client import (
    FilmIds,
    LiveLetterboxdClient,
    parse_film_ids,
    parse_list_page,
)
from plexdb.letterboxd_lists import MAX_LIST_ENTRIES, refresh_letterboxd_lists
from plexdb.list_decisions import canonical_list_url, list_source
from plexdb.lists import list_detail, list_table
from plexdb.store import init as init_store
from plexdb.store import open_store

COLLECTION_ID = f"letterboxd:{LIST_USER}/{LIST_SLUG}"
PAGE_1 = f"/{LIST_USER}/list/{LIST_SLUG}/"
PAGE_2 = f"{PAGE_1}page/2/"
ALL_MOVIES = {slug: FilmIds("movie", str(100 + n)) for n, (slug, _, _) in enumerate(LIST_FILMS)}


def _open(store: Path) -> Any:
    init_store(store)
    return open_store(store)


def _decide(conn: sqlite3.Connection, url: str, decision: str, *, added: int = 0) -> None:
    conn.execute(
        "INSERT INTO list_decision (url, decision, decided_at, added) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (url) DO UPDATE SET decision = excluded.decision, "
        "decided_at = excluded.decided_at, added = excluded.added",
        (url, decision, datetime.now(UTC).isoformat(timespec="seconds"), added),
    )
    conn.commit()


def _age(conn: sqlite3.Connection) -> None:
    """Make every stored list's fetch older than any decision made after this call."""
    conn.execute("UPDATE collection SET observed_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()


def _seed(conn: sqlite3.Connection, item_id: str, item_type: str, title: str, tmdb: str) -> None:
    conn.execute(
        "INSERT INTO items (item_id, type, title) VALUES (?, ?, ?)", (item_id, item_type, title)
    )
    conn.execute(
        "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
        "VALUES (?, 'tmdb', ?, ?, '2026-01-01T00:00:00+00:00')",
        (item_id, tmdb, item_type),
    )
    conn.commit()


def _entries(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT rank, ns, value, kind, title, year FROM collection_entry ORDER BY rank"
        )
    ]


def _rows(conn: sqlite3.Connection) -> tuple[int, int, int]:
    return tuple(
        conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
        for t in ("collection", "collection_entry", "collection_membership")
    )


# ---- URLs -------------------------------------------------------------------------------


def test_a_letterboxd_list_url_has_one_canonical_spelling() -> None:
    for spelling in (
        f"https://letterboxd.com/{LIST_USER}/list/{LIST_SLUG}/",
        f"http://www.letterboxd.com/{LIST_USER.upper()}/list/{LIST_SLUG}/page/2/",
    ):
        assert canonical_list_url(spelling) == LIST_URL
        assert list_source(spelling) == "letterboxd"
    assert list_source("https://mdblist.com/lists/lina/top") == "mdblist"
    assert canonical_list_url("https://letterboxd.com/lina/films/") is None
    assert canonical_list_url("https://letterboxd.com/lina/list/x/likes/") is None


# ---- parsing ----------------------------------------------------------------------------


def test_a_list_page_gives_every_entry_in_order_with_its_slug_title_and_year() -> None:
    page = parse_list_page(load("list_page_1.html"))

    assert page is not None
    assert page.name == "feeling lost in your 20's"
    assert page.size == 5
    assert page.next_path == PAGE_2
    assert [(f.slug, f.title, f.year) for f in page.entries] == list(LIST_FILMS[:3])
    last = parse_list_page(load("list_page_2.html"))
    assert last is not None and last.next_path is None
    assert [(f.slug, f.title, f.year) for f in last.entries] == list(LIST_FILMS[3:])


def test_a_page_without_the_list_marker_is_not_a_list_page() -> None:
    challenge = "<html><head><title>Just a moment...</title></head><body>wait</body></html>"
    no_body_class = load("list_page_1.html").replace("list-page", "film-page")
    no_heading = load("list_page_1.html").replace("title-1", "title-9")

    assert parse_list_page(challenge) is None
    assert parse_list_page(no_body_class) is None
    assert parse_list_page(no_heading) is None


def test_a_film_page_gives_its_tmdb_id_scoped_by_media_type() -> None:
    assert parse_film_ids(film_page()) == FilmIds("movie", "27205")
    assert parse_film_ids(film_page("tv", "1396")) == FilmIds("show", "1396")
    assert parse_film_ids(film_page("movie", None)) == FilmIds("movie", None)
    assert parse_film_ids("<html><body>Just a moment...</body></html>") is None


# ---- the client -------------------------------------------------------------------------


def _site(**overrides: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    pages = {
        PAGE_1: httpx.Response(200, text=load("list_page_1.html")),
        PAGE_2: httpx.Response(200, text=load("list_page_2.html")),
        "/film/juno/": httpx.Response(200, text=film_page("movie", "7326")),
        "/film/moved/": httpx.Response(301, headers={"Location": "/film/juno/"}),
    }
    pages.update({k.replace("__", "/"): v for k, v in overrides.items()})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=load("robots.txt"))
        return pages.get(request.url.path, httpx.Response(404))

    return handler


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> LiveLetterboxdClient:
    return LiveLetterboxdClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None
    )


def test_the_client_reads_every_page_of_a_list_and_nothing_disallowed() -> None:
    client = _client(_site())

    contents = client.list_contents(LIST_USER, LIST_SLUG)

    assert contents.pages == 2 and contents.size == 5
    assert [(f.slug, f.title, f.year) for f in contents.entries] == list(LIST_FILMS)
    assert client.requested == ["/robots.txt", PAGE_1, PAGE_2]


def test_a_cloudflare_challenge_fails_the_list() -> None:
    client = _client(lambda r: httpx.Response(403) if r.url.path != "/robots.txt" else _robots())

    with pytest.raises(LetterboxdError, match="403 .* Cloudflare challenge"):
        client.list_contents(LIST_USER, LIST_SLUG)


def _robots() -> httpx.Response:
    return httpx.Response(200, text=load("robots.txt"))


def test_a_200_page_without_the_list_marker_fails_the_list() -> None:
    client = _client(
        lambda r: (
            _robots()
            if r.url.path == "/robots.txt"
            else httpx.Response(
                200, text="<html><title>Just a moment...</title><body></body></html>"
            )
        )
    )

    with pytest.raises(LetterboxdError, match="no list marker"):
        client.list_contents(LIST_USER, LIST_SLUG)


def test_a_next_link_off_the_list_is_never_followed() -> None:
    hostile = load("list_page_1.html").replace(PAGE_2, "/somewhere/else/")
    client = _client(_site(**{PAGE_1.replace("/", "__"): httpx.Response(200, text=hostile)}))

    with pytest.raises(LetterboxdError, match="not the next page"):
        client.list_contents(LIST_USER, LIST_SLUG)
    assert "/somewhere/else/" not in client.requested


def test_a_film_page_resolves_through_a_rename_redirect() -> None:
    client = _client(_site())

    assert client.film_ids("juno") == FilmIds("movie", "7326")
    assert client.film_ids("moved") == FilmIds("movie", "7326")
    with pytest.raises(LetterboxdError, match="Letterboxd returned 404"):
        client.film_ids("missing")


def test_a_film_page_without_the_film_marker_fails() -> None:
    client = _client(_site(**{"__film__juno__": httpx.Response(200, text="<body>Just a moment")}))

    with pytest.raises(LetterboxdError, match="no film marker"):
        client.film_ids("juno")


# ---- the harvest ------------------------------------------------------------------------


def test_an_added_letterboxd_list_is_stored_whole_with_owned_entries_resolved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = RecordedListSource(ids=ALL_MOVIES)
    with _open(tmp_path / "plexdb.db") as conn:
        _seed(conn, "imdb:tt0478311", "movie", "Juno", "104")
        _decide(conn, LIST_URL, "added", added=1)

        stats = refresh_letterboxd_lists(conn, source)

        assert stats.lists_fetched == 1 and stats.entries_stored == 5
        assert conn.execute(
            "SELECT collection_id, source, name, url, size, provenance FROM collection"
        ).fetchone()[:] == (
            COLLECTION_ID,
            "letterboxd",
            "feeling lost in your 20's",
            LIST_URL,
            5,
            "added",
        )
        assert _entries(conn) == [
            (n, "tmdb", str(100 + n - 1), "movie", title, year)
            for n, (_, title, year) in enumerate(LIST_FILMS, start=1)
        ]
        assert [
            tuple(r) for r in conn.execute("SELECT item_id, rank FROM collection_membership")
        ] == [("imdb:tt0478311", 5)]
        detail = list_detail(conn, LIST_URL + "/")
        assert detail is not None
        assert [(e["rank"], e["owned"], e["title"]) for e in detail["entries"]] == [
            (1, False, "Scott Pilgrim vs. the World"),
            (2, False, "Millennium Mambo"),
            (3, False, "The Devil, Probably"),
            (4, False, "Me and Earl and the Dying Girl"),
            (5, True, "Juno"),
        ]
        assert [(r["source"], r["author"], r["owned"]) for r in list_table(conn)] == [
            ("letterboxd", LIST_USER, 1)
        ]
    assert f"letterboxd: {LIST_URL} -> HTTP 200, 2 page(s), 5 entries parsed, 1 resolved" in (
        capsys.readouterr().out
    )


def test_a_show_and_a_movie_sharing_a_tmdb_number_are_not_confused(tmp_path: Path) -> None:
    ids = dict(ALL_MOVIES, juno=FilmIds("show", "104"))
    source = RecordedListSource(ids=ids)
    with _open(tmp_path / "plexdb.db") as conn:
        _seed(conn, "imdb:tt0000001", "movie", "Not Juno", "104")
        _decide(conn, LIST_URL, "added", added=1)

        refresh_letterboxd_lists(conn, source)

        assert conn.execute("SELECT COUNT(*) FROM collection_membership").fetchone()[0] == 0
        assert ("tmdb", "104", "show") in {(r[1], r[2], r[3]) for r in _entries(conn)}


def test_a_film_with_no_tmdb_match_is_stored_under_its_slug(tmp_path: Path) -> None:
    ids = dict(ALL_MOVIES, juno=FilmIds("movie", None))
    with _open(tmp_path / "plexdb.db") as conn:
        _decide(conn, LIST_URL, "added", added=1)

        refresh_letterboxd_lists(conn, RecordedListSource(ids=ids))

        assert _entries(conn)[-1] == (5, "letterboxd", "juno", "movie", "Juno", 2007)


def test_a_film_shared_between_lists_is_read_once_per_run(tmp_path: Path) -> None:
    source = RecordedListSource(ids=ALL_MOVIES)
    other = "https://letterboxd.com/pierce/list/other"
    with _open(tmp_path / "plexdb.db") as conn:
        _decide(conn, LIST_URL, "added", added=1)
        _decide(conn, other, "added", added=1)

        refresh_letterboxd_lists(conn, source)

        assert sorted(source.film_calls) == sorted(slug for slug, _, _ in LIST_FILMS)
        assert len(source.list_calls) == 2


def test_a_failed_fetch_records_the_reason_and_writes_no_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    failing = LetterboxdError("Letterboxd answered 403 for /x/ (a Cloudflare challenge)")
    with _open(tmp_path / "plexdb.db") as conn:
        _decide(conn, LIST_URL, "added", added=1)

        stats = refresh_letterboxd_lists(conn, RecordedListSource(failing=failing))

        assert stats.lists_failed == 1 and stats.lists_fetched == 0
        assert _rows(conn) == (0, 0, 0)
        assert [
            tuple(r) for r in conn.execute("SELECT list_key, attempts, reason FROM list_attempt")
        ] == [(LIST_URL, 1, "Letterboxd answered 403 for /x/ (a Cloudflare challenge)")]
        row = next(r for r in list_table(conn) if r["url"] == LIST_URL)
        assert (row["source"], row["state"], row["failure"]["attempts"]) == (
            "letterboxd",
            "failed",
            1,
        )
    assert f"letterboxd: {LIST_URL} -> failed: Letterboxd answered 403" in capsys.readouterr().out


def test_a_film_page_that_will_not_load_fails_the_list_and_keeps_the_stored_one(
    tmp_path: Path,
) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        _decide(conn, LIST_URL, "added", added=1)
        refresh_letterboxd_lists(conn, RecordedListSource(ids=ALL_MOVIES))
        before = (_rows(conn), _entries(conn))
        _age(conn)
        _decide(conn, LIST_URL, "refetch")
        ids = {k: v for k, v in ALL_MOVIES.items() if k != "juno"}

        stats = refresh_letterboxd_lists(conn, RecordedListSource(ids=ids))

        assert stats.lists_failed == 1
        assert (_rows(conn), _entries(conn)) == before


def test_fewer_entries_read_than_the_list_holds_is_a_failure_not_a_partial_list(
    tmp_path: Path,
) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        _decide(conn, LIST_URL, "added", added=1)

        stats = refresh_letterboxd_lists(conn, RecordedListSource(ids=ALL_MOVIES, size=216))

        assert stats.lists_failed == 1 and _rows(conn) == (0, 0, 0)
        reason = conn.execute("SELECT reason FROM list_attempt").fetchone()[0]
        assert reason == "read 5 of the 216 entries the list says it holds"


def test_a_list_over_the_entry_cap_is_refused_whole(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr("plexdb.letterboxd_lists.MAX_LIST_ENTRIES", 4)
    source = RecordedListSource(ids=ALL_MOVIES)
    with _open(tmp_path / "plexdb.db") as conn:
        _decide(conn, LIST_URL, "added", added=1)

        stats = refresh_letterboxd_lists(conn, source)

        assert stats.lists_failed == 1 and _rows(conn) == (0, 0, 0)
        assert source.film_calls == []
    assert MAX_LIST_ENTRIES == 2000


def test_a_fresh_list_is_not_fetched_again_and_three_failures_stop_it(tmp_path: Path) -> None:
    source = RecordedListSource(ids=ALL_MOVIES)
    with _open(tmp_path / "plexdb.db") as conn:
        _decide(conn, LIST_URL, "added", added=1)
        refresh_letterboxd_lists(conn, source)

        stats = refresh_letterboxd_lists(conn, source)
        assert stats.lists_cached == 1 and len(source.list_calls) == 1

        failing = RecordedListSource(failing=LetterboxdError("down"))
        _age(conn)
        for _ in range(3):
            _decide(conn, LIST_URL, "refetch")
            refresh_letterboxd_lists(conn, failing)
        _decide(conn, LIST_URL, "refetch")
        stopped = refresh_letterboxd_lists(conn, failing)
        assert stopped.lists_stopped == 1 and len(failing.list_calls) == 3


def test_excluding_a_letterboxd_list_wipes_it_and_it_is_never_fetched(tmp_path: Path) -> None:
    source = RecordedListSource(ids=ALL_MOVIES)
    with _open(tmp_path / "plexdb.db") as conn:
        _decide(conn, LIST_URL, "added", added=1)
        refresh_letterboxd_lists(conn, source)
        _decide(conn, LIST_URL, "excluded")

        stats = refresh_letterboxd_lists(conn, source, stale_days=0)

        assert _rows(conn) == (0, 0, 0)
        assert (stats.lists_excluded, stats.lists_removed, stats.lists_seen) == (1, 1, 0)
        assert len(source.list_calls) == 1


def test_a_re_pull_replaces_the_entries_rather_than_appending(tmp_path: Path) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        _decide(conn, LIST_URL, "added", added=1)
        refresh_letterboxd_lists(conn, RecordedListSource(ids=ALL_MOVIES))
        _age(conn)
        _decide(conn, LIST_URL, "refetch")

        refresh_letterboxd_lists(conn, RecordedListSource(ids=ALL_MOVIES, size=4, films=4))

        assert [e[0] for e in _entries(conn)] == [1, 2, 3, 4]
        assert conn.execute("SELECT size FROM collection").fetchone()[0] == 4


def test_the_two_harvests_each_ignore_the_others_decisions(tmp_path: Path) -> None:
    mdb = FakeMDBListSource()
    with _open(tmp_path / "plexdb.db") as conn:
        _decide(conn, LIST_URL, "added", added=1)
        _decide(conn, "https://mdblist.com/lists/pierce/mine", "added", added=1)

        stats = refresh_mdblist(conn, mdb)

        assert (stats.lists_seen, stats.lists_failed) == (1, 1)
        assert mdb.find_calls == [("pierce", "mine")]
        source = RecordedListSource(ids=ALL_MOVIES)
        refresh_letterboxd_lists(conn, source)
        assert source.list_calls == [(LIST_USER, LIST_SLUG)]


def test_lists_failing_in_a_row_stop_the_run_before_every_list_loses_an_attempt(
    tmp_path: Path,
) -> None:
    source = RecordedListSource(failing=LetterboxdError("Letterboxd answered 403"))
    with _open(tmp_path / "plexdb.db") as conn:
        for n in range(5):
            _decide(conn, f"https://letterboxd.com/pierce/list/l{n}", "added", added=1)

        stats = refresh_letterboxd_lists(conn, source)

        assert (stats.lists_seen, stats.lists_failed) == (3, 3)
        assert len(source.list_calls) == 3
        assert conn.execute("SELECT COUNT(*) FROM list_attempt").fetchone()[0] == 3
