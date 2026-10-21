"""`enrich_tmdb_language` against recorded TMDB details responses, and the
"English original, no English subtitle" recipe over a fixture store."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from tmdb_fixtures import load

from plexdb.enrich_tmdb_language import NAMESPACE, SOURCE, enrich_tmdb_language, wipe
from plexdb.explore import RECIPES, run_query
from plexdb.store import init, open_store
from plexdb.tmdb_client import LiveTMDbClient

_RECORDED = {
    "/3/movie/155": "movie_155_details.json",
    "/3/movie/129": "movie_129_details.json",
    "/3/tv/1396": "tv_1396_details.json",
}


class _Recorded:
    """A `LiveTMDbClient` over recorded details responses that counts requests."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        http = httpx.Client(transport=httpx.MockTransport(self._handler))
        self.client = LiveTMDbClient("the-test-key", http=http)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        return httpx.Response(200, json=load(_RECORDED[request.url.path]))


def _item(conn: sqlite3.Connection, item_id: str, kind: str, title: str, **cols: str) -> None:
    conn.execute(
        "INSERT INTO items (item_id, type, title, show_item_id) VALUES (?, ?, ?, ?)",
        (item_id, kind, title, cols.get("show")),
    )
    if "tmdb" in cols:
        conn.execute(
            "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
            "VALUES (?, 'tmdb', ?, ?, '2026-01-01T00:00:00+00:00')",
            (item_id, cols["tmdb"], kind),
        )


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    store = tmp_path / "plexdb.db"
    init(store)
    with open_store(store) as connection:
        _item(connection, "m:dark-knight", "movie", "The Dark Knight", tmdb="155")
        _item(connection, "m:spirited-away", "movie", "Spirited Away", tmdb="129")
        _item(connection, "s:breaking-bad", "show", "Breaking Bad", tmdb="1396")
        _item(connection, "m:no-id", "movie", "No Id")
        connection.commit()
        yield connection


def _languages(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["item_id"]: r["value"]
        for r in conn.execute(
            "SELECT item_id, value FROM enrichment "
            "WHERE namespace = ? AND source = ? AND key = 'original_language'",
            (NAMESPACE, SOURCE),
        )
    }


def test_a_movie_and_a_show_each_get_their_original_language(conn: sqlite3.Connection) -> None:
    recorded = _Recorded()

    stats = enrich_tmdb_language(conn, recorded.client)

    assert _languages(conn) == {
        "m:dark-knight": "en",
        "m:spirited-away": "ja",
        "s:breaking-bad": "en",
    }
    assert (stats.titles_fetched, stats.languages_written, stats.titles_skipped_no_tmdb_id) == (
        3,
        3,
        1,
    )
    assert sorted(recorded.requests) == ["/3/movie/129", "/3/movie/155", "/3/tv/1396"]


def test_a_second_run_inside_the_threshold_makes_no_request(conn: sqlite3.Connection) -> None:
    recorded = _Recorded()
    enrich_tmdb_language(conn, recorded.client)
    recorded.requests.clear()

    stats = enrich_tmdb_language(conn, recorded.client)

    assert recorded.requests == []
    assert (stats.titles_fetched, stats.titles_cached) == (0, 3)
    assert len(_languages(conn)) == 3


def test_wipe_removes_the_rows_and_cursors_so_the_next_run_refetches(
    conn: sqlite3.Connection,
) -> None:
    recorded = _Recorded()
    enrich_tmdb_language(conn, recorded.client)

    assert wipe(conn) == 6  # three rows, three cursors
    recorded.requests.clear()
    enrich_tmdb_language(conn, recorded.client)

    assert len(recorded.requests) == 3


def _part(conn: sqlite3.Connection, part_id: int, item_id: str, *subtitle_langs: str) -> None:
    conn.execute(
        "INSERT INTO media_part (part_id, item_id, rating_key, file, fetched_at) "
        "VALUES (?, ?, ?, ?, '2026-01-01T00:00:00+00:00')",
        (part_id, item_id, str(part_id), f"/media/{part_id}.mkv"),
    )
    for idx, lang in enumerate(subtitle_langs):
        conn.execute(
            "INSERT INTO media_stream (part_id, idx, kind, lang, forced) "
            "VALUES (?, ?, 'subtitle', ?, 0)",
            (part_id, idx, lang),
        )


def test_the_recipe_returns_english_originals_with_no_english_subtitle(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    languages = {
        "m:subbed": "en",
        "m:bare": "en",
        "m:french-subs": "en",
        "m:anime": "ja",
        "m:unwalked": "en",
        "s:breaking-bad": "en",
        "s:spirited-show": "ja",
    }
    for item_id in languages:
        if not item_id.startswith("s:"):
            _item(conn, item_id, "movie", item_id)
    _item(conn, "s:spirited-show", "show", "Spirited Show")
    _item(conn, "e:1", "episode", "Pilot", show="s:breaking-bad")
    _item(conn, "e:2", "episode", "Cat's in the Bag", show="s:breaking-bad")
    _item(conn, "e:j", "episode", "Elsewhere", show="s:spirited-show")
    for item_id, language in languages.items():
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
            "VALUES (?, ?, ?, 'original_language', ?, '2026-01-01T00:00:00+00:00')",
            (item_id, NAMESPACE, SOURCE, language),
        )
    _part(conn, 1, "m:subbed", "eng", "fre")
    _part(conn, 2, "m:bare")
    _part(conn, 3, "m:french-subs", "fre")
    _part(conn, 4, "m:anime")
    _part(conn, 5, "e:1", "spa")
    _part(conn, 6, "e:2", "eng")
    _part(conn, 7, "e:j")
    conn.commit()
    recipe = next(r for r in RECIPES if r["name"] == "English original, no English subtitle")
    assert recipe["group"] == "Library health"

    store = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    result: Any = run_query(store, recipe["sql"])

    titles = [row[result["columns"].index("item_id")] for row in result["rows"]]
    assert sorted(titles) == ["e:1", "m:bare", "m:french-subs"]
