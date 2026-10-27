"""Plex DB-EX reports keyword counts the way a keyword-cosine scorer sees them.

Driven through `build_index`, `titles_tagged` and the HTTP server a browser
talks to, against a small store whose counts can be worked out by hand.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fixture_store import init, publish, refresh_map, refresh_tagnetwork
from playwright.sync_api import Browser, Page, sync_playwright

from plexdb import explore, schedule
from plexdb.decisions import EXPLORE_SAVED_PATH_VAR
from plexdb.errors import ConfigError, StoreError
from plexdb.explore import (
    EXPLORE_PORT_VAR,
    RECIPES,
    QueryError,
    RegionLabel,
    SavedQueries,
    build_index,
    keyword_fingerprint,
    make_server,
    network_fingerprint,
    region_labels,
    run_query,
    stored_map,
    stored_network,
    tag_network,
    title_keywords_json,
    title_map,
    titles_tagged,
)
from plexdb.merge_review import merge_map
from plexdb.names import display_labels
from plexdb.panels import PanelsFile, series_key
from plexdb.store import open_readonly, open_store

FETCHED = "2026-08-09T12:00:00+00:00"

# Four movies and a show. `stinger` rides along with `superhero` on two of the
# three superhero films, which is the shape Plex DB-EX exists to expose.
TITLES = {
    "imdb:tt1": ("movie", "Iron Man", 2008, ["superhero", "stinger", "based on comic"]),
    "imdb:tt2": ("movie", "Thor", 2011, ["superhero", "stinger"]),
    "imdb:tt3": ("movie", "Unbreakable", 2000, ["superhero"]),
    "imdb:tt4": ("movie", "Heat", 1995, ["heist"]),
    "tvdb:9": ("show", "Daredevil", 2015, ["superhero", "lawyer"]),
}


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "plexdb.db"
    init(path)
    with open_store(path) as conn:
        # Rows go in descending key order, so a scan that skips the sort hands
        # the keyword digest's aggregate the rows backwards.
        for item_id, (kind, title, year, keywords) in reversed(TITLES.items()):
            conn.execute(
                "INSERT INTO items (item_id, type, title, year) VALUES (?, ?, ?, ?)",
                (item_id, kind, title, year),
            )
            for keyword in reversed(keywords):
                conn.execute(
                    "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
                    "VALUES (?, 'keywords', 'tmdb', 'keyword', ?, ?)",
                    (item_id, keyword, FETCHED),
                )
        # A movie with no keywords is not part of N, just as a pool candidate
        # with no facts is not part of taste-cosine's doc_count.
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('fs:x', 'movie', 'Bare')")
        conn.commit()
    return path


def test_counts_idf_and_co_tags_are_per_media_type(store: Path) -> None:
    with open_readonly(store) as conn:
        movies = build_index(conn, "movie")
        shows = build_index(conn, "show")

    assert movies.titles == 4
    by_value = {tag.value: tag for tag in movies.tags}
    assert [tag.value for tag in movies.tags][:2] == ["superhero", "stinger"]
    assert by_value["superhero"].df == 3
    assert by_value["stinger"].idf == pytest.approx(1 + math.log(4 / 2))
    # Two titles are too few to trust at the chips' evidence (CO_STRENGTH): stinger's 2 of
    # 2 with superhero has a cautious lift of 0.44, under chance, so no chip.
    assert by_value["stinger"].related == ()
    assert "lawyer" not in by_value

    assert shows.titles == 1
    assert {tag.value for tag in shows.tags} == {"superhero", "lawyer"}


def test_tag_network_positions_by_shared_titles_and_links_co_tags(store: Path) -> None:
    with open_readonly(store) as conn:
        net = tag_network(conn, "movie", min_df=1)
        without = tag_network(conn, "movie", min_df=1, exclude=frozenset({"stinger"}))

    assert {n.value: n.df for n in net.nodes} == {
        "superhero": 3,
        "stinger": 2,
        "based on comic": 1,
        "heist": 1,
    }
    assert net.edges == (
        ("stinger", "superhero", 2),
        ("based on comic", "stinger", 1),
        ("based on comic", "superhero", 1),
    )
    # A show's keyword never joins a movie network.
    assert "lawyer" not in {n.value for n in net.nodes}
    assert {n.value: n.df for n in without.nodes} == {
        "superhero": 3,
        "based on comic": 1,
        "heist": 1,
    }
    assert without.edges == (("based on comic", "superhero", 1),)
    assert all(0.0 <= n.x <= 1.0 and 0.0 <= n.y <= 1.0 for n in net.nodes)


def test_embedding_scales_both_axes_by_one_factor_without_pinning_the_rim() -> None:
    """Nothing piles onto the border: each axis reaches 0 at one point, only the
    longer axis reaches 1, and the shorter keeps its share of the longer's span."""
    rows, cols = 200, 12
    cells_r = [i for i in range(rows) for _ in range(3)]
    cells_c = [(i * m + k) % cols for i in range(rows) for k, m in enumerate((1, 5, 7))]
    cells_v = [1.0 + (i % 4) for i in range(rows) for _ in range(3)]
    coords = explore._embed_2d(rows, cols, (cells_r, cells_c, cells_v), seed=0)
    assert coords.shape == (rows, 2)
    for axis in (0, 1):
        column = coords[:, axis]
        assert column.min() == 0.0
        assert (column == 0.0).sum() == 1
    tops = sorted(coords.max(axis=0))
    assert tops[1] == 1.0
    assert 0.0 < tops[0] < 1.0
    assert (coords == 1.0).sum() == 1


def test_title_map_places_alike_titles_together(tmp_path: Path) -> None:
    """Twenty westerns and twenty space films, each drawing from its own pool of
    tags plus one shared packaging tag: every title's nearest neighbour on the
    map must be the same kind of film."""
    path = tmp_path / "plexdb.db"
    init(path)
    pools = {
        "western": ["cowboy", "frontier", "sheriff", "outlaw", "horse", "desert"],
        "space": ["spaceship", "alien", "astronaut", "planet", "robot", "galaxy"],
    }
    with open_store(path) as conn:
        for genre, pool in pools.items():
            for i in range(20):
                item_id = f"tmdb:{genre}{i}"
                conn.execute(
                    "INSERT INTO items (item_id, type, title) VALUES (?, 'movie', ?)",
                    (item_id, f"{genre} {i}"),
                )
                for keyword in [*(pool[(i + k) % 6] for k in range(3)), "stinger"]:
                    conn.execute(
                        "INSERT INTO enrichment "
                        "(item_id, namespace, source, key, value, fetched_at) "
                        "VALUES (?, 'keywords', 'tmdb', 'keyword', ?, ?)",
                        (item_id, keyword, FETCHED),
                    )
        conn.commit()

    with open_readonly(path) as conn:
        tmap = title_map(conn, "movie", exclude=frozenset({"stinger"}))

    assert len(tmap.points) == 40 and tmap.unplaced == 0
    assert all(0.0 <= p.x <= 1.0 and 0.0 <= p.y <= 1.0 for p in tmap.points)
    for point in tmap.points:
        nearest = min(
            (other for other in tmap.points if other is not point),
            key=lambda other: math.dist((point.x, point.y), (other.x, other.y)),
        )
        assert ("western" in nearest.item_id) == ("western" in point.item_id), point.item_id


def _islands(path: Path) -> list[tuple[str, float, float]]:
    """Forty movies on two islands, one per genre, each tagged with its genre's
    stemmed word, the western island also with a packaging tag. Returns where
    each sits, `(item_id, x, y)`."""
    init(path)
    points: list[tuple[str, float, float]] = []
    with open_store(path) as conn:
        for genre, stem, x0 in (("western", "outlaw", 0.0), ("space", "galaxi", 0.9)):
            for i in range(20):
                item_id = f"tmdb:{genre}{i}"
                conn.execute(
                    "INSERT INTO items (item_id, type, title) VALUES (?, 'movie', ?)",
                    (item_id, item_id),
                )
                points.append((item_id, x0 + (i % 5) * 0.02, (i // 5) * 0.02))
                # The western island's packaging tag out-scores its genre tag,
                # so it names the region until it is excluded.
                own = [stem] if genre == "space" or i < 15 else []
                for keyword in (*own, *(["stinger"] if genre == "western" else [])):
                    conn.execute(
                        "INSERT INTO enrichment "
                        "(item_id, namespace, source, key, value, fetched_at) "
                        "VALUES (?, 'keywords', 'tmdb', 'keyword', ?, ?)",
                        (item_id, keyword, FETCHED),
                    )
        conn.execute("INSERT INTO keyword_forms (surface, keyword) VALUES ('galaxies', 'galaxi')")
        conn.commit()
    return points


def test_region_labels_name_each_region_by_its_own_tag_and_skip_noise(tmp_path: Path) -> None:
    """The labels name the islands by keyword and never use the excluded tag."""
    path = tmp_path / "plexdb.db"
    points = _islands(path)

    with open_readonly(path) as conn:
        labels = region_labels(conn, "movie", points, frozenset({"stinger"}))
        with_noise = region_labels(conn, "movie", points)

    assert {label.tag for label in labels} == {"outlaw", "galaxi"}
    assert {label.tag for label in with_noise} == {"stinger", "galaxi"}
    depths = {(label.node + 1).bit_length() - 1 for label in labels}
    assert min(depths) == explore.LABEL_FIRST_DEPTH


def test_titles_tagged_lists_one_media_type_by_title(store: Path) -> None:
    with open_readonly(store) as conn:
        titles = titles_tagged(conn, "movie", "superhero")

    assert [(t.title, t.year, t.keywords) for t in titles] == [
        ("Iron Man", 2008, 3),
        ("Thor", 2011, 2),
        ("Unbreakable", 2000, 1),
    ]


def test_a_keyword_two_sources_both_list_counts_once(store: Path) -> None:
    """`superhero` on Iron Man from a second source (ADR-0016's `source`
    column lets both rows exist) must not inflate `build_index`'s per-tag
    title count, nor duplicate Iron Man in `titles_tagged`, nor add to its own
    reported keyword count."""
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
            "VALUES ('imdb:tt1', 'keywords', 'mdblist', 'keyword', 'superhero', ?)",
            (FETCHED,),
        )
        conn.commit()

    with open_readonly(store) as conn:
        movies = build_index(conn, "movie")
        titles = titles_tagged(conn, "movie", "superhero")

    by_value = {tag.value: tag for tag in movies.tags}
    assert by_value["superhero"].df == 3, "still three titles, not four"
    assert [(t.title, t.keywords) for t in titles] == [
        ("Iron Man", 3),
        ("Thor", 2),
        ("Unbreakable", 1),
    ], "Iron Man appears once, with its keyword count unchanged by the second source"


def test_title_keywords_json_carries_stored_form_and_readable_spelling(store: Path) -> None:
    """A stemmed keyword like `stinger` reads fine as-is, but a title card still
    needs both the stored form (to click, search and count by) and a readable
    spelling from `keyword_forms` when one is on file."""
    with open_store(store) as conn:
        conn.execute("INSERT INTO keyword_forms (surface, keyword) VALUES ('stingers', 'stinger')")
        conn.commit()

    with open_readonly(store) as conn:
        keywords = title_keywords_json(conn, "imdb:tt1", display_labels(conn, {}))

    assert keywords == [
        {"value": "based on comic", "label": "based on comic"},
        {"value": "stinger", "label": "stingers"},
        {"value": "superhero", "label": "superhero"},
    ]


@pytest.fixture
def base_url(store: Path) -> Iterator[str]:
    server = make_server(store, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def _last_ndjson(body: bytes) -> Any:
    """The final line of an `/api/tagnetwork` NDJSON body — the `"done"` stage,
    whether it arrived as the only line (a cache hit) or the last of several
    (a live compute)."""
    lines = [line for line in body.split(b"\n") if line.strip()]
    return json.loads(lines[-1])


def test_server_serves_the_page_and_both_endpoints(base_url: str) -> None:
    status, page = _get(f"{base_url}/")
    assert status == 200 and b"Plex DB-EX" in page

    status, body = _get(f"{base_url}/api/tags?kind=movie")
    tags = json.loads(body)
    assert status == 200 and tags["titles"] == 4
    assert tags["tags"][1] == {
        "value": "stinger",
        "df": 2,
        "idf": round(1 + math.log(2), 4),
        "related": [],
    }

    # Every tag sharing a movie with superhero (df 3 of N 4), most shared first, each with
    # its own df: the counts the page's evidence slider computes cautious lifts from.
    status, body = _get(f"{base_url}/api/cotags?kind=movie&tag=superhero")
    cotags = json.loads(body)
    assert status == 200 and cotags.pop("stamp") == tags["stamp"]
    assert cotags == {
        "kind": "movie",
        "tag": "superhero",
        "df": 3,
        "titles": 4,
        "co": [["stinger", 2, 2], ["based on comic", 1, 1]],
    }
    status, body = _get(f"{base_url}/api/cotags?kind=movie&tag=lawyer")
    assert status == 200 and json.loads(body)["co"] == []
    assert _get(f"{base_url}/api/cotags?kind=movie")[0] == 400

    status, body = _get(f"{base_url}/api/titles?kind=show&tag=superhero")
    assert status == 200 and [t["title"] for t in json.loads(body)["titles"]] == ["Daredevil"]

    # Only "superhero" (df 3) clears NETWORK_MIN_DF in this four-movie store.
    status, body = _get(f"{base_url}/api/tagnetwork?kind=movie")
    net = _last_ndjson(body)
    assert status == 200 and [n[0] for n in net["nodes"]] == ["superhero"]
    assert net["edges"] == []

    status, body = _get(f"{base_url}/api/tagnetwork?kind=movie&exclude=superhero")
    assert _last_ndjson(body)["nodes"] == []

    status, body = _get(f"{base_url}/api/map?kind=movie")
    tmap = json.loads(body)
    assert status == 200 and len(tmap["points"]) + tmap["unplaced"] == 4

    status, body = _get(f"{base_url}/api/title?item_id=imdb:tt1")
    assert json.loads(body)["keywords"] == [
        {"value": "based on comic", "label": "based on comic"},
        {"value": "stinger", "label": "stinger"},
        {"value": "superhero", "label": "superhero"},
    ]


def test_title_endpoint_returns_the_card_with_its_nearest_titles_by_keyword_cosine(
    base_url: str, store: Path
) -> None:
    """Iron Man shares superhero and stinger with Thor, only superhero with
    Unbreakable, and nothing with Heat; Daredevil is a show. A TMDB similar edge
    no longer decides anything."""
    with open_store(store) as conn:
        conn.execute(
            "UPDATE items SET studio = 'Marvel', content_rating = 'PG-13' "
            "WHERE item_id = 'imdb:tt1'"
        )
        conn.execute(
            "INSERT INTO edges (from_id, to_id, edge_type, rank, fetched_at) "
            "VALUES ('imdb:tt1', 'imdb:tt4', 'tmdb_similar', 1, ?)",
            (FETCHED,),
        )
        conn.commit()

    status, body = _get(f"{base_url}/api/title?item_id=imdb:tt1")
    card = json.loads(body)
    assert status == 200
    assert (card["title"], card["year"], card["studio"], card["content_rating"]) == (
        "Iron Man",
        2008,
        "Marvel",
        "PG-13",
    )
    hero, stinger, comic = 1 + math.log(4 / 3), 1 + math.log(4 / 2), 1 + math.log(4 / 1)
    iron_man = math.sqrt(hero**2 + stinger**2 + comic**2)
    thor = (hero**2 + stinger**2) / (iron_man * math.sqrt(hero**2 + stinger**2))
    unbreakable = hero**2 / (iron_man * hero)
    assert card["similar"] == [
        {"item_id": "imdb:tt2", "title": "Thor", "year": 2011, "score": round(thor, 4)},
        {
            "item_id": "imdb:tt3",
            "title": "Unbreakable",
            "year": 2000,
            "score": round(unbreakable, 4),
        },
    ]


def test_title_endpoint_refuses_a_missing_unknown_or_oversize_id(base_url: str) -> None:
    assert _get(f"{base_url}/api/title")[0] == 400
    assert _get(f"{base_url}/api/title?item_id=" + "x" * 201)[0] == 400
    status, body = _get(f"{base_url}/api/title?item_id=imdb:nope")
    assert status == 404 and "imdb:nope" in json.loads(body)["error"]


def test_title_endpoint_refuses_a_request_over_the_concurrency_cap(base_url: str) -> None:
    held = [explore.TITLE_SLOTS.acquire(blocking=False) for _ in range(4)]
    try:
        status, body = _get(f"{base_url}/api/title?item_id=imdb:tt1")
        assert status == 429 and "already running" in json.loads(body)["error"]
    finally:
        for _ in filter(None, held):
            explore.TITLE_SLOTS.release()
    assert _get(f"{base_url}/api/title?item_id=imdb:tt1")[0] == 200


def test_titles_endpoint_refuses_a_request_over_the_concurrency_cap(base_url: str) -> None:
    held = [explore.TITLE_SLOTS.acquire(blocking=False) for _ in range(4)]
    try:
        status, body = _get(f"{base_url}/api/titles?kind=movie&tag=superhero")
        assert status == 429 and "already running" in json.loads(body)["error"]
    finally:
        for _ in filter(None, held):
            explore.TITLE_SLOTS.release()
    assert _get(f"{base_url}/api/titles?kind=movie&tag=superhero")[0] == 200


def _add_keyword(store: Path, item_id: str, keyword: str) -> None:
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
            "VALUES (?, 'keywords', 'tmdb', 'keyword', ?, '2026-09-01T00:00:00+00:00')",
            (item_id, keyword),
        )
        conn.commit()


def test_the_stored_map_carries_its_labels_and_the_page_shows_each_keywords_display_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`refresh-map` stores the labels `region_labels` gives for the drawn
    positions; `/api/map` serves them, current or stale, without labelling
    again, and shows each keyword's readable spelling, or its name once a
    person gives it one — on a map drawn live too."""
    path = tmp_path / "plexdb.db"
    _islands(path)
    refresh_map(path)
    with open_store(path) as conn:
        payload = stored_map(conn, "movie")
        assert payload is not None
        placed = [(p[0], p[3], p[4]) for p in cast(list[list[Any]], payload["points"])]
        assert payload["labels"] == region_labels(conn, "movie", placed)
        assert "galaxi" in {label.tag for label in cast(list[RegionLabel], payload["labels"])}

    def refuse(*_args: object) -> None:
        raise AssertionError("a stored map was labelled again")

    monkeypatch.setattr(explore, "region_labels", refuse)
    server = make_server(path, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/map?kind=movie"
        _, body = _get(url)
        shown = {r[7]: r[6] for r in json.loads(body)["labels"]}
        assert shown.pop("galaxi") == "galaxies"
        assert all(text == tag for tag, text in shown.items())
        with open_store(path) as conn:
            conn.execute(
                "INSERT INTO keyword_names (keyword, name, decided_at) "
                "VALUES ('galaxi', 'Deep Space', '2026-10-12T00:00:00+00:00')"
            )
            conn.commit()
        _, body = _get(url)
        assert ("galaxi", "Deep Space") in {(r[7], r[6]) for r in json.loads(body)["labels"]}

        with open_store(path) as conn:
            conn.execute("UPDATE title_map_state SET fingerprint = 'older' WHERE kind = 'movie'")
            conn.commit()
        _, body = _get(f"{url}&stored=1")
        stale = json.loads(body)
        assert stale["stale"] is True
        assert ("galaxi", "Deep Space") in {(r[7], r[6]) for r in stale["labels"]}

        monkeypatch.undo()
        _, body = _get(f"{url}&exclude=stinger")
        live = {r[7]: r[6] for r in json.loads(body)["labels"]}
        assert live.get("galaxi") == "Deep Space" and "stinger" not in live
    finally:
        server.shutdown()
        server.server_close()


def test_refresh_stores_both_kinds_and_skips_a_kind_whose_keywords_are_unchanged(
    store: Path,
) -> None:
    first = refresh_map(store)
    with open_store(store) as conn:
        counts = conn.execute("SELECT kind, COUNT(*) FROM title_map GROUP BY kind")
        rows = [tuple(r) for r in counts]
    again = refresh_map(store)

    assert [(r.kind, r.redrawn) for r in first] == [("movie", True), ("show", True)]
    # Heat's only tag is on no other movie, so it is left off; so is the one show.
    assert rows == [("movie", 3)]
    assert [(r.kind, r.redrawn, r.count) for r in again] == [
        ("movie", False, 3),
        ("show", False, 0),
    ]

    _add_keyword(store, "tvdb:9", "vigilante")
    changed = refresh_map(store)
    assert [(r.kind, r.redrawn) for r in changed] == [("movie", False), ("show", True)]


@pytest.mark.parametrize(
    "rewrite",
    [
        # Same row count, same titles, same newest fetched_at: one row re-stamped older.
        "UPDATE enrichment SET fetched_at = '2026-01-01T00:00:00+00:00' "
        "WHERE item_id = 'imdb:tt2' AND value = 'stinger'",
        # Same counts and stamps: one keyword swapped for another.
        "UPDATE enrichment SET value = 'vigilante' WHERE item_id = 'imdb:tt3'",
        # A one-keyword title leaves the kind while another one-keyword title joins it.
        "DELETE FROM enrichment WHERE item_id = 'tvdb:9' AND value = 'lawyer'; "
        "UPDATE items SET type = 'show' WHERE item_id = 'imdb:tt3'; "
        "UPDATE items SET type = 'movie' WHERE item_id = 'tvdb:9'",
    ],
    ids=["older-stamp", "swapped-keyword", "type-flip"],
)
def test_a_same_size_rewrite_of_the_keyword_data_makes_the_stored_map_stale(
    store: Path, rewrite: str
) -> None:
    refresh_map(store)
    refresh_tagnetwork(store)
    with open_store(store) as conn:
        before = keyword_fingerprint(conn, "movie"), network_fingerprint(conn, "movie")
        assert stored_map(conn, "movie") is not None
        assert stored_network(conn, "movie") is not None

        conn.executescript(rewrite)
        conn.commit()

        assert keyword_fingerprint(conn, "movie") != before[0]
        assert network_fingerprint(conn, "movie") != before[1]
        assert stored_map(conn, "movie") is None
        assert stored_network(conn, "movie") is None


def test_a_refresh_committing_mid_read_does_not_reach_the_stored_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "plexdb.db"
    _islands(path)
    refresh_map(path)
    fingerprint = explore.keyword_fingerprint

    def refresh_meanwhile(conn: sqlite3.Connection, kind: str) -> str:
        # Runs after the state is read and before the points and labels are.
        with open_store(path) as writer:
            writer.execute("UPDATE title_map SET x = x + 100")
            writer.execute("UPDATE title_map_label SET value = 'redrawn'")
            writer.commit()
        return fingerprint(conn, kind)

    monkeypatch.setattr(explore, "keyword_fingerprint", refresh_meanwhile)
    with open_readonly(path) as conn:
        payload = stored_map(conn, "movie")
        assert payload is not None
        assert all(p[3] < 100 for p in cast(list[list[Any]], payload["points"]))
        assert "redrawn" not in {label.tag for label in cast(list[RegionLabel], payload["labels"])}
        assert not conn.in_transaction


def test_the_stored_map_read_leaves_a_writers_open_transaction_open(tmp_path: Path) -> None:
    path = tmp_path / "plexdb.db"
    _islands(path)
    refresh_map(path)
    with open_store(path) as conn:
        conn.execute("UPDATE title_map_label SET value = 'pending'")
        payload = stored_map(conn, "movie")
        assert payload is not None
        assert {label.tag for label in cast(list[RegionLabel], payload["labels"])} == {"pending"}
        assert conn.in_transaction
        conn.rollback()
        after = stored_map(conn, "movie")
        assert after is not None
        assert "pending" not in {label.tag for label in cast(list[RegionLabel], after["labels"])}


def test_keyword_fingerprint_is_the_digest_the_rust_writer_stores(store: Path) -> None:
    """The fingerprint is a published rule (`crates/plexdb/src/keyword_views.rs`): the writer
    stores it and this page recomputes it, so the two must make the same bytes."""

    def framed(sha: Any, value: object) -> None:
        if value is None:
            sha.update(b"\x00")
        else:
            data = str(value).encode()
            sha.update(b"\x01" + len(data).to_bytes(4, "big") + data)

    refresh_map(store)
    with open_store(store) as conn:
        keywords = hashlib.sha256()
        for row in conn.execute(
            "SELECT e.item_id, e.source, e.value, e.fetched_at "
            "FROM enrichment e JOIN items i USING (item_id) "
            "WHERE e.namespace = 'keywords' AND e.key = 'keyword' AND i.type = 'movie' "
            "ORDER BY e.item_id, e.source, e.value"
        ):
            for field in row:
                framed(keywords, field)
        merges = hashlib.sha256()
        for pair in sorted(merge_map(conn).items()):
            for field in pair:
                framed(merges, field)
        expected = f"{explore.MAP_RECIPE}|{keywords.hexdigest()}|{merges.hexdigest()}"
        assert keyword_fingerprint(conn, "movie") == expected
        (stored,) = conn.execute(
            "SELECT fingerprint FROM title_map_state WHERE kind = 'movie'"
        ).fetchone()
        assert stored == expected


def test_a_merge_verdict_folds_one_keyword_into_another_in_every_view_and_redraws_the_map(
    store: Path,
) -> None:
    with open_store(store) as conn:
        values = [tag.value for tag in build_index(conn, "movie").tags]
        a, b = values[0], values[1]
        before = keyword_fingerprint(conn, "movie")
        conn.execute(
            "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_score, jev_model, judged_at) "
            "VALUES (?, ?, 0.95, 'jev-1', '2026-10-09T00:00:00Z')",
            tuple(sorted((a, b))),
        )
        conn.commit()
        after = build_index(conn, "movie")
        tagged = {t.item_id for t in titles_tagged(conn, "movie", a)}
        rows = conn.execute(
            "SELECT DISTINCT item_id FROM enrichment JOIN items USING (item_id) "
            "WHERE value IN (?, ?) AND type = 'movie'",
            (a, b),
        ).fetchall()
        assert keyword_fingerprint(conn, "movie") != before
    assert a in {tag.value for tag in after.tags}
    assert b not in {tag.value for tag in after.tags}
    assert tagged == {r[0] for r in rows}
    with open_store(store) as conn:
        card = [k["value"] for k in title_keywords_json(conn, "imdb:tt2", {})]
    assert a in card and b not in card


def test_map_endpoint_serves_the_stored_map_and_draws_live_once_it_is_stale(
    store: Path, base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    refresh_map(store)
    drawn: list[frozenset[str]] = []
    real = explore.title_map

    def counting(conn, kind, exclude=frozenset(), seed=0):  # type: ignore[no-untyped-def]
        drawn.append(exclude)
        return real(conn, kind, exclude, seed)

    monkeypatch.setattr(explore, "title_map", counting)

    _, body = _get(f"{base_url}/api/map?kind=movie")
    stored = json.loads(body)
    assert drawn == []
    assert sorted(p[1] for p in stored["points"]) == ["Iron Man", "Thor", "Unbreakable"]

    _get(f"{base_url}/api/map?kind=movie&exclude=stinger")
    assert drawn == [frozenset({"stinger"})]

    _add_keyword(store, "imdb:tt4", "superhero")
    _, body = _get(f"{base_url}/api/map?kind=movie")
    assert drawn == [frozenset({"stinger"}), frozenset()]
    assert len(json.loads(body)["points"]) == 4


def test_map_endpoint_answers_a_stale_stored_map_at_once_when_asked(
    store: Path, base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    refresh_map(store)
    drawn: list[frozenset[str]] = []
    real = explore.title_map

    def counting(conn, kind, exclude=frozenset(), seed=0):  # type: ignore[no-untyped-def]
        drawn.append(exclude)
        return real(conn, kind, exclude, seed)

    monkeypatch.setattr(explore, "title_map", counting)

    _, body = _get(f"{base_url}/api/map?kind=movie&stored=1")
    assert json.loads(body)["stale"] is False
    _add_keyword(store, "imdb:tt4", "superhero")

    _, body = _get(f"{base_url}/api/map?kind=movie&stored=1")
    old = json.loads(body)
    assert drawn == []
    assert old["stale"] is True and len(old["points"]) == 3

    _, body = _get(f"{base_url}/api/map?kind=movie")
    fresh = json.loads(body)
    assert drawn == [frozenset()]
    assert fresh["stale"] is False and len(fresh["points"]) == 4


def test_map_cache_computes_one_key_once_across_concurrent_requests(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    drawn: list[frozenset[str]] = []
    real = explore.title_map

    def slow(conn, kind, exclude=frozenset(), seed=0):  # type: ignore[no-untyped-def]
        drawn.append(exclude)
        started.set()
        assert release.wait(120)
        return real(conn, kind, exclude, seed)

    monkeypatch.setattr(explore, "title_map", slow)
    cache = explore._MapCache(store)
    key = frozenset({"stinger"})
    results: list[dict[str, object]] = []
    threads = [
        threading.Thread(target=lambda: results.append(cache.get("movie", key))) for _ in range(2)
    ]
    for t in threads:
        t.start()
    assert started.wait(120)
    release.set()
    for t in threads:
        t.join(120)

    assert drawn == [key]
    assert len(results) == 2 and results[0] is results[1]


def test_map_cache_does_not_hold_one_key_behind_another(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    movie_started = threading.Event()
    release = threading.Event()
    real = explore.title_map

    def gated(conn, kind, exclude=frozenset(), seed=0):  # type: ignore[no-untyped-def]
        if kind == "movie":
            movie_started.set()
            assert release.wait(120)
        return real(conn, kind, exclude, seed)

    monkeypatch.setattr(explore, "title_map", gated)
    maps = explore._MapCache(store)
    tags = explore._IndexCache(store)
    slow = threading.Thread(target=lambda: maps.get("movie", frozenset({"stinger"})))
    slow.start()
    try:
        assert movie_started.wait(120)
        # The movie draw stays gated throughout, so these can only finish if
        # they do not share its lock.
        shown = threading.Event()

        def other_keys() -> None:
            maps.get("show", frozenset())
            tags.get("show")
            shown.set()

        threading.Thread(target=other_keys, daemon=True).start()
        assert shown.wait(60)
    finally:
        release.set()
        slow.join(120)


def test_tagnetwork_refresh_stores_both_kinds_and_skips_a_kind_whose_keywords_are_unchanged(
    store: Path,
) -> None:
    first = refresh_tagnetwork(store)
    with open_store(store) as conn:
        counts = conn.execute("SELECT kind, COUNT(*) FROM tag_network GROUP BY kind")
        rows = [tuple(r) for r in counts]
    again = refresh_tagnetwork(store)

    assert [(r.kind, r.redrawn) for r in first] == [("movie", True), ("show", True)]
    # Only "superhero" (df=3) clears the default df >= 3 floor among the movie tags.
    assert rows == [("movie", 1)]
    assert [(r.kind, r.redrawn, r.count) for r in again] == [
        ("movie", False, 1),
        ("show", False, 0),
    ]

    _add_keyword(store, "tvdb:9", "vigilante")
    changed = refresh_tagnetwork(store)
    assert [(r.kind, r.redrawn) for r in changed] == [("movie", False), ("show", True)]


def test_tagnetwork_endpoint_serves_the_stored_network_and_draws_live_once_it_is_stale(
    store: Path, base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    refresh_tagnetwork(store)
    drawn: list[frozenset[str]] = []
    real = explore.tag_network_streaming

    def counting(  # type: ignore[no-untyped-def]
        conn,
        kind,
        exclude,
        emit,
        min_df=explore.NETWORK_MIN_DF,
        edges_per_node=explore.NETWORK_EDGES_PER_NODE,
        seed=0,
    ):
        drawn.append(exclude)
        return real(conn, kind, exclude, emit, min_df, edges_per_node, seed)

    monkeypatch.setattr(explore, "tag_network_streaming", counting)

    # The default network is precomputed, so this line is the only one on the
    # wire — a cache hit never calls tag_network_streaming.
    _, body = _get(f"{base_url}/api/tagnetwork?kind=movie")
    stored = _last_ndjson(body)
    assert drawn == []
    assert [n[0] for n in stored["nodes"]] == ["superhero"]

    _, body = _get(f"{base_url}/api/tagnetwork?kind=movie&exclude=stinger")
    assert drawn == [frozenset({"stinger"})]
    stages = [json.loads(line)["stage"] for line in body.split(b"\n") if line.strip()]
    assert stages == ["vocab", "edges", "done"]

    _add_keyword(store, "imdb:tt4", "superhero")
    _get(f"{base_url}/api/tagnetwork?kind=movie")
    assert drawn == [frozenset({"stinger"}), frozenset()]


def test_tagnetwork_cache_streams_stages_to_the_winner_and_one_line_to_the_waiter(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two browser tabs asking for the same exclude set trigger one live draw,
    not two: the second call waits behind the first and is handed the same
    finished network as a single "done" line, per plex-db-ex-nm5."""
    started = threading.Event()
    release = threading.Event()
    calls = 0
    real = explore.tag_network_streaming

    def slow(  # type: ignore[no-untyped-def]
        conn,
        kind,
        exclude,
        emit,
        min_df=explore.NETWORK_MIN_DF,
        edges_per_node=explore.NETWORK_EDGES_PER_NODE,
        seed=0,
    ):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(60)
        return real(conn, kind, exclude, emit, min_df, edges_per_node, seed)

    monkeypatch.setattr(explore, "tag_network_streaming", slow)
    networks = explore._TagNetworkCache(store)
    key = frozenset({"stinger"})
    stages: list[list[str]] = [[], []]

    def fetch(i: int) -> None:
        networks.get_streaming("movie", key, lambda msg: stages[i].append(str(msg["stage"])))

    threads = [threading.Thread(target=fetch, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    assert started.wait(60)
    release.set()
    for t in threads:
        t.join(60)

    assert calls == 1
    # One thread won the flight and streamed all three real stages; the other
    # waited behind it and was handed only the finished "done" line.
    assert sorted(stages) == [["done"], ["vocab", "edges", "done"]]


@pytest.mark.parametrize(
    "failure",
    [StoreError("store is damaged"), PermissionError("denied"), FileNotFoundError()],
)
def test_tagnetwork_error_line_reads_the_same_as_a_json_route_error(
    base_url: str, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(explore, "title_map", fail)
    monkeypatch.setattr(explore, "tag_network_streaming", fail)

    _, body = _get(f"{base_url}/api/map?kind=movie")
    expected = json.loads(body)["error"]
    status, body = _get(f"{base_url}/api/tagnetwork?kind=movie")

    # The stream's 200 is already sent when the draw fails; only the text carries over.
    assert status == 200
    assert _last_ndjson(body) == {"stage": "error", "error": expected}


def test_sqlite_error_answers_503_on_a_json_route_and_on_the_stream(
    base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(explore, "title_map", fail)
    monkeypatch.setattr(explore, "tag_network_streaming", fail)

    status, body = _get(f"{base_url}/api/map?kind=movie")
    expected = "could not read the store: database disk image is malformed"
    assert (status, json.loads(body)["error"]) == (503, expected)

    status, body = _get(f"{base_url}/api/tagnetwork?kind=movie")
    assert status == 200
    assert _last_ndjson(body) == {"stage": "error", "error": expected}


@pytest.mark.parametrize(
    "failure",
    [
        sqlite3.OperationalError("database is locked"),
        sqlite3.DatabaseError("database disk image is malformed"),
    ],
)
def test_store_read_errors_map_to_503(failure: sqlite3.Error) -> None:
    assert explore._error_reply(failure, Path("store.db")) == (
        f"could not read the store: {failure}",
        HTTPStatus.SERVICE_UNAVAILABLE,
    )


@pytest.mark.parametrize(
    "failure",
    [
        sqlite3.ProgrammingError("Cannot operate on a closed database."),
        sqlite3.InterfaceError("bad parameter"),
    ],
)
def test_sqlite_code_bugs_stay_unmapped(failure: sqlite3.Error) -> None:
    assert explore._error_reply(failure, Path("store.db")) is None


def test_unmapped_stream_failure_is_logged_to_stderr(
    base_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("layout exploded")

    monkeypatch.setattr(explore, "tag_network_streaming", fail)

    _, body = _get(f"{base_url}/api/tagnetwork?kind=movie")

    assert _last_ndjson(body) == {"stage": "error", "error": "layout exploded"}
    assert "RuntimeError: layout exploded" in capsys.readouterr().err


def test_store_damage_is_logged_on_a_json_route_and_on_the_stream(
    base_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(explore, "title_map", fail)
    monkeypatch.setattr(explore, "tag_network_streaming", fail)

    status, _ = _get(f"{base_url}/api/map?kind=movie")
    err = capsys.readouterr().err
    assert status == 503
    assert "store problem" in err and "malformed" in err

    _get(f"{base_url}/api/tagnetwork?kind=movie")
    assert "store problem" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("failure", "logged"),
    [
        (StoreError("damaged"), True),
        (sqlite3.OperationalError("database is locked"), True),
        (PermissionError("denied"), True),
        (FileNotFoundError(), False),
        (BrokenPipeError(), False),
        (explore.SavedQueriesError("bad file"), False),
        (sqlite3.ProgrammingError("closed"), False),
        (ValueError("bad request"), False),
    ],
)
def test_only_store_damage_is_logged(
    failure: Exception, logged: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    explore._log_store_damage(failure)

    assert ("store problem" in capsys.readouterr().err) is logged


def test_a_missing_store_is_not_logged(
    base_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError()

    monkeypatch.setattr(explore, "title_map", fail)

    status, _ = _get(f"{base_url}/api/map?kind=movie")

    assert status == 503
    assert capsys.readouterr().err == ""


def test_index_cache_recomputes_when_the_store_changes(store: Path) -> None:
    tags = explore._IndexCache(store)
    before = tags.get("show")
    assert tags.get("show") is before

    _add_keyword(store, "tvdb:9", "vigilante")
    after = tags.get("show")
    assert after is not before
    assert "vigilante" in [t["value"] for t in after["tags"]]  # type: ignore[attr-defined]


def test_keyed_cache_retries_after_a_failed_compute_without_overlapping() -> None:
    cache = explore._KeyedCache(keep=2)
    started = threading.Event()
    release = threading.Event()
    running = 0
    peak = 0
    calls = 0
    guard = threading.Lock()

    def compute() -> dict[str, object]:
        nonlocal running, peak, calls
        with guard:
            calls += 1
            first = calls == 1
            running += 1
            peak = max(peak, running)
        try:
            if first:
                started.set()
                assert release.wait(60)
                raise RuntimeError("draw failed")
            return {"ok": True}
        finally:
            with guard:
                running -= 1

    errors: list[BaseException] = []
    results: list[dict[str, object]] = []

    def request() -> None:
        try:
            results.append(cache.get("k", 1, compute))
        except RuntimeError as err:
            errors.append(err)

    owner = threading.Thread(target=request)
    owner.start()
    assert started.wait(60)
    waiter = threading.Thread(target=request)
    waiter.start()
    release.set()
    owner.join(60)
    waiter.join(60)

    assert len(errors) == 1 and results == [{"ok": True}]
    assert peak == 1


def test_server_refuses_what_it_cannot_answer(base_url: str) -> None:
    status, body = _get(f"{base_url}/api/tags?kind=episode")
    assert status == 400 and "episode" in json.loads(body)["error"]
    assert _get(f"{base_url}/api/titles?kind=movie")[0] == 400
    assert _get(f"{base_url}/api/tagnetwork?kind=episode")[0] == 400
    assert _get(f"{base_url}/api/nope")[0] == 404


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_scheduler_serves_the_snapshot_once_one_is_published(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the host Plex DB-EX runs inside `plexdb schedule` and reads the
    snapshot. Before the first sweep publishes one it answers 503, not a
    traceback; after, it serves the snapshot's numbers without a restart."""
    snapshot = tmp_path / "snapshot" / "plexdb.snapshot.db"
    port = _free_port()
    monkeypatch.setenv(schedule.SCHEDULE_VAR, "03:30")
    monkeypatch.setenv(EXPLORE_PORT_VAR, str(port))
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("PLEXDB_SNAPSHOT_PATH", str(snapshot))

    schedule.run_scheduler(migrate=lambda: None, extras=lambda: None, iterations=0)

    url = f"http://127.0.0.1:{port}/api/tags?kind=movie"
    assert _get(url)[0] == 503
    publish(store, snapshot)
    status, body = _get(url)
    assert status == 200 and json.loads(body)["titles"] == 4


def test_scheduler_refuses_an_explore_port_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(schedule.SCHEDULE_VAR, "03:30")
    monkeypatch.setenv(EXPLORE_PORT_VAR, "http")
    with pytest.raises(ConfigError, match=EXPLORE_PORT_VAR):
        schedule.run_scheduler(migrate=lambda: None, extras=lambda: None, iterations=0)


def _send(method: str, url: str, body: object | None = None) -> tuple[int, dict[str, object]]:
    data = None if body is None else json.dumps(body).encode()
    headers = {} if data is None else {"Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM items",
        "INSERT INTO items (item_id, type, title) VALUES ('x', 'movie', 'X')",
        "DROP TABLE items",
        "ATTACH DATABASE ':memory:' AS other",
        "PRAGMA journal_mode = DELETE",
        "SELECT 1; SELECT 2",
    ],
)
def test_query_refuses_anything_but_one_select(store: Path, sql: str) -> None:
    with pytest.raises(QueryError):
        run_query(store, sql)
    with open_readonly(store) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 6


def test_query_returns_columns_rows_and_marks_blobs(store: Path) -> None:
    out = run_query(store, "SELECT title, x'0102' AS b FROM items WHERE item_id = 'imdb:tt4'")
    assert out["columns"] == ["title", "b"]
    assert out["rows"] == [["Heat", "<blob 2 bytes>"]]
    assert out["row_count"] == 1 and out["truncated"] is False


def test_query_labels_item_ids_in_the_result_and_skips_the_rest(store: Path) -> None:
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO items (item_id, type, title, show_title, show_item_id, season, episode)"
            " VALUES ('plex:e1', 'episode', 'Pilot', 'Daredevil', 'tvdb:9', 1, 2)"
        )
        conn.commit()
    out = run_query(
        store,
        "SELECT item_id FROM items WHERE item_id IN ('imdb:tt4', 'tvdb:9', 'plex:e1', 'fs:x') "
        "UNION ALL SELECT 'tmdb:0'",
    )
    labels = out["labels"]
    assert isinstance(labels, dict)
    assert labels["imdb:tt4"] == "Heat (1995)"
    assert labels["tvdb:9"] == "Daredevil (2015)"
    assert labels["plex:e1"] == "Daredevil S01E02: Pilot"
    assert labels["fs:x"] == "Bare"
    assert "tmdb:0" not in labels


def test_query_gives_a_string_that_is_not_id_shaped_no_label(store: Path) -> None:
    with open_store(store) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('raw', 'movie', 'Raw')")
        conn.commit()
    out = run_query(store, "SELECT item_id, title FROM items WHERE item_id IN ('raw', 'imdb:tt4')")
    assert out["labels"] == {"imdb:tt4": "Heat (1995)"}


def test_query_cuts_at_the_row_cap_and_says_so(store: Path) -> None:
    out = run_query(store, "SELECT item_id FROM items", rows=4)
    assert out["row_count"] == 4 and out["truncated"] is True
    out = run_query(store, "SELECT item_id FROM items", rows=6)
    assert out["row_count"] == 6 and out["truncated"] is False


def test_query_connection_pins_temp_store_to_file(store: Path) -> None:
    """A large sort spills to a temp file rather than growing the process's own
    memory (plex-db-ex-oyg.4) — measured at 26 MB peak RSS with this pinned
    versus 864 MB with `temp_store = MEMORY`, against the deploy base image."""
    with open_readonly(store) as conn:
        explore._configure_query_connection(conn)
        assert tuple(conn.execute("PRAGMA temp_store").fetchone()) == (1,)


def test_query_stops_a_runaway_recursive_cte(store: Path) -> None:
    runaway = (
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT COUNT(*) FROM n"
    )
    with pytest.raises(QueryError, match="stopped after"):
        run_query(store, runaway, seconds=0.2)


def test_query_refuses_a_value_over_the_length_limit(store: Path) -> None:
    with pytest.raises(QueryError, match="too big"):
        run_query(store, f"SELECT randomblob({explore.QUERY_VALUE_BYTES + 1})")


def test_query_refuses_a_third_statement_while_two_run(store: Path) -> None:
    held = [explore.QUERY_SLOTS.acquire(blocking=False) for _ in range(2)]
    try:
        with pytest.raises(explore.TooManyQueries, match="already running"):
            run_query(store, "SELECT 1")
    finally:
        for _ in filter(None, held):
            explore.QUERY_SLOTS.release()
    assert run_query(store, "SELECT 1")["row_count"] == 1


def test_every_recipe_runs_against_the_schema(store: Path) -> None:
    for recipe in RECIPES:
        run_query(store, recipe["sql"])


def test_dashboards_return_each_seeded_panel_with_its_live_rows_and_its_series(
    base_url: str, store: Path
) -> None:
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO media_part (part_id, item_id, rating_key, file, fetched_at) "
            "VALUES (1, 'imdb:tt1', '1', '/m/1.mkv', '2026-01-01T00:00:00+00:00')"
        )
        conn.executemany(
            "INSERT INTO media_stream (part_id, idx, kind, lang, forced) VALUES (1, ?, ?, ?, 0)",
            [(0, "audio", "eng"), (1, "audio", "jpn"), (2, "subtitle", "eng")],
        )
        conn.commit()
    seeded = PanelsFile(store.parent / "scratch-panels.json").read_or_seed()
    key = series_key(next(p for p in seeded[0].panels if p.id == "tracks-per-language"))
    (store.parent / "panel-history.json").write_text(
        json.dumps(
            {
                "series": {
                    key: [{"day": "2026-10-20", "value": 2}, {"day": "2026-10-21", "error": "x"}]
                }
            }
        )
    )

    status, body = _send("GET", f"{base_url}/api/dashboards")

    assert status == 200
    dashboards = cast("list[dict[str, Any]]", body["dashboards"])
    assert [d["name"] for d in dashboards] == ["Library health"]
    panels = {p["id"]: p for p in dashboards[0]["panels"]}
    assert list(panels) == [
        "damaged-files",
        "english-no-english-subtitle",
        "decode-progress",
        "tracks-per-language",
    ]
    assert panels["tracks-per-language"]["live"] == {
        "columns": ["lang", "tracks"],
        "rows": [["eng", 2], ["jpn", 1]],
        "truncated": False,
    }
    assert panels["tracks-per-language"]["series"] == [
        {"day": "2026-10-20", "value": 2},
        {"day": "2026-10-21", "error": "x"},
    ]
    assert panels["decode-progress"]["live"]["rows"] == [[0, 1, None]]
    assert panels["decode-progress"]["total"] == "parts_total"
    assert panels["damaged-files"]["series"] == []
    assert (store.parent / "explore-panels.json").exists()


def test_a_dashboard_panel_whose_sql_fails_carries_the_error_not_rows(
    base_url: str, store: Path
) -> None:
    broken = {
        "id": "p",
        "title": "P",
        "sql": "SELECT nope FROM items",
        "chart": "line",
        "value": "rows",
        "width": 1,
        "height": "short",
    }
    (store.parent / "explore-panels.json").write_text(
        json.dumps({"dashboards": [{"id": "d", "name": "D", "panels": [broken]}]})
    )

    _, body = _send("GET", f"{base_url}/api/dashboards")

    live = body["dashboards"][0]["panels"][0]["live"]  # type: ignore[index]
    assert "no such column" in live["error"] and "rows" not in live


def test_an_empty_dashboards_file_stays_empty(base_url: str, store: Path) -> None:
    (store.parent / "explore-panels.json").write_text(json.dumps({"dashboards": []}))

    assert _send("GET", f"{base_url}/api/dashboards") == (200, {"dashboards": []})


def _panel_ids(base_url: str) -> list[str]:
    _, body = _send("GET", f"{base_url}/api/dashboards")
    return [p["id"] for p in body["dashboards"][0]["panels"]]  # type: ignore[index]


def test_panel_writes_create_replace_move_and_delete(base_url: str, store: Path) -> None:
    mk: dict[str, object] = {"title": "T", "sql": "SELECT 1 AS n", "chart": "line", "value": "rows"}
    mk |= {"width": 2, "height": "short"}

    status, body = _send(
        "PUT", f"{base_url}/api/panels", {"dashboard_id": "library-health", "panel": mk}
    )
    assert status == 200
    new_id = body["panel"]["id"]  # type: ignore[index]
    assert len(new_id) == 32
    assert _panel_ids(base_url)[-1] == new_id

    replaced = {
        **mk,
        "id": new_id,
        "title": "T2",
        "chart": "gauge",
        "total": "n",
        "value": "column:n",
    }
    status, body = _send(
        "PUT", f"{base_url}/api/panels", {"dashboard_id": "library-health", "panel": replaced}
    )
    assert status == 200 and body["panel"]["title"] == "T2"  # type: ignore[index]
    assert _panel_ids(base_url).count(new_id) == 1

    status, body = _send(
        "POST",
        f"{base_url}/api/panels/move",
        {"dashboard_id": "library-health", "panel_id": new_id, "to": 0},
    )
    assert status == 200 and body["panel_ids"][0] == new_id  # type: ignore[index]
    assert _panel_ids(base_url)[0] == new_id

    history = store.parent / "panel-history.json"
    history.write_text(
        json.dumps(
            {
                "series": {
                    f"{new_id}:aaaaaaaaaaaa": [{"day": "2026-10-20", "value": 1}],
                    "damaged-files:bbbbbbbbbbbb": [{"day": "2026-10-20", "value": 2}],
                }
            }
        )
    )
    status, body = _send("DELETE", f"{base_url}/api/panels/{new_id}")
    assert status == 200 and body["series_dropped"] == 1
    assert new_id not in _panel_ids(base_url)
    assert list(json.loads(history.read_text())["series"]) == ["damaged-files:bbbbbbbbbbbb"]


def test_a_panel_save_with_failing_sql_is_refused_and_writes_nothing(
    base_url: str, store: Path
) -> None:
    _send("GET", f"{base_url}/api/dashboards")
    before = (store.parent / "explore-panels.json").read_text()
    panel: dict[str, object] = {"title": "T", "sql": "SELECT nope FROM items", "chart": "line"}
    panel["value"] = "rows"
    panel |= {"width": 1, "height": "short"}

    status, body = _send(
        "PUT", f"{base_url}/api/panels", {"dashboard_id": "library-health", "panel": panel}
    )

    assert status == 400 and "no such column" in str(body["error"])
    assert (store.parent / "explore-panels.json").read_text() == before
    ok = {**panel, "sql": "SELECT 1 AS n", "value": "column:missing"}
    status, body = _send(
        "PUT", f"{base_url}/api/panels", {"dashboard_id": "library-health", "panel": ok}
    )
    assert status == 400 and "no column missing" in str(body["error"])


def test_panel_writes_name_what_they_cannot_find(base_url: str) -> None:
    _send("GET", f"{base_url}/api/dashboards")
    panel: dict[str, object] = {"title": "T", "sql": "SELECT 1", "chart": "line"}
    panel |= {"value": "rows", "width": 1}
    panel |= {"height": "short"}

    assert (
        _send("PUT", f"{base_url}/api/panels", {"dashboard_id": "nope", "panel": panel})[0] == 404
    )
    ghost = {"dashboard_id": "library-health", "panel": {**panel, "id": "ghost"}}
    assert _send("PUT", f"{base_url}/api/panels", ghost)[0] == 404
    assert _send("DELETE", f"{base_url}/api/panels/ghost")[0] == 404
    bad_to = {"dashboard_id": "library-health", "panel_id": "damaged-files", "to": 9}
    assert _send("POST", f"{base_url}/api/panels/move", bad_to)[0] == 400
    no_total = {"dashboard_id": "library-health", "panel": {**panel, "chart": "gauge"}}
    status, body = _send("PUT", f"{base_url}/api/panels", no_total)
    assert status == 400 and "needs text total" in str(body["error"])


def _dashboards(base_url: str) -> list[dict[str, Any]]:
    _, body = _send("GET", f"{base_url}/api/dashboards")
    return body["dashboards"]  # type: ignore[return-value]


def test_dashboards_are_created_renamed_and_deleted_with_their_series(
    base_url: str, store: Path
) -> None:
    _dashboards(base_url)

    status, body = _send("PUT", f"{base_url}/api/dashboards", {"name": "  Second  "})
    assert status == 200
    second = body["dashboard"]["id"]  # type: ignore[index]
    assert len(second) == 32 and body["dashboard"]["name"] == "Second"  # type: ignore[index]
    assert [(d["id"], len(d["panels"])) for d in _dashboards(base_url)] == [
        ("library-health", 4),
        (second, 0),
    ]

    status, body = _send("PUT", f"{base_url}/api/dashboards", {"id": second, "name": "Renamed"})
    assert status == 200 and body["dashboard"] == {"id": second, "name": "Renamed"}
    assert [d["name"] for d in _dashboards(base_url)] == ["Library health", "Renamed"]

    panel: dict[str, object] = {"title": "T", "sql": "SELECT 1", "chart": "line", "value": "rows"}
    panel |= {"width": 1, "height": "short"}
    _, saved = _send("PUT", f"{base_url}/api/panels", {"dashboard_id": second, "panel": panel})
    pid = saved["panel"]["id"]  # type: ignore[index]
    history = store.parent / "panel-history.json"
    history.write_text(
        json.dumps(
            {
                "series": {
                    f"{pid}:aaaaaaaaaaaa": [{"day": "2026-10-20", "value": 1}],
                    f"{pid}:bbbbbbbbbbbb": [{"day": "2026-10-21", "value": 1}],
                    "damaged-files:cccccccccccc": [{"day": "2026-10-20", "value": 2}],
                }
            }
        )
    )

    status, body = _send("DELETE", f"{base_url}/api/dashboards/{second}")
    assert status == 200
    assert body == {"deleted": second, "panels": 1, "series_dropped": 2}
    assert [d["id"] for d in _dashboards(base_url)] == ["library-health"]
    assert list(json.loads(history.read_text())["series"]) == ["damaged-files:cccccccccccc"]


def test_the_last_dashboard_cannot_be_deleted_and_bad_dashboard_writes_are_named(
    base_url: str, store: Path
) -> None:
    _dashboards(base_url)
    before = (store.parent / "explore-panels.json").read_text()

    status, body = _send("DELETE", f"{base_url}/api/dashboards/library-health")
    assert status == 400 and "last dashboard" in str(body["error"])
    assert (store.parent / "explore-panels.json").read_text() == before

    assert _send("DELETE", f"{base_url}/api/dashboards/ghost")[0] == 404
    assert _send("PUT", f"{base_url}/api/dashboards", {"id": "ghost", "name": "X"})[0] == 404
    assert _send("PUT", f"{base_url}/api/dashboards", {"name": "   "})[0] == 400
    assert _send("PUT", f"{base_url}/api/dashboards", {"name": "x" * 81})[0] == 400
    assert _send("PUT", f"{base_url}/api/dashboards", {"id": 3, "name": "X"})[0] == 400
    assert (store.parent / "explore-panels.json").read_text() == before


def test_the_library_health_group_is_gone_from_the_recipes(base_url: str) -> None:
    _, body = _send("GET", f"{base_url}/api/recipes")

    assert "Library health" not in {r["group"] for r in body["recipes"]}  # type: ignore[attr-defined]


def test_saved_queries_round_trip_through_the_server(base_url: str, store: Path) -> None:
    url = f"{base_url}/api/saved"
    assert _send("GET", url) == (200, {"queries": []})

    entry = {"name": "heat", "sql": "SELECT 1", "note": "n"}
    assert _send("PUT", url, entry) == (200, {"queries": [entry]})
    edited = {**entry, "sql": "SELECT 2"}
    assert _send("PUT", url, edited)[1] == {"queries": [edited]}
    assert _send("GET", url)[1] == {"queries": [edited]}
    assert (store.parent / "explore-queries.json").exists()

    assert _send("DELETE", f"{url}?name=heat") == (200, {"queries": []})
    assert _send("DELETE", f"{url}?name=heat")[0] == 404
    assert _send("PUT", url, {"name": "", "sql": "SELECT 1"})[0] == 400


def test_saved_queries_refuses_past_the_entry_cap(tmp_path: Path) -> None:
    saved = SavedQueries(tmp_path / "explore-queries.json")
    for n in range(saved.MAX_ENTRIES):
        saved.upsert(f"q{n}", "SELECT 1", "")
    with pytest.raises(ValueError, match="too many saved queries"):
        saved.upsert("one too many", "SELECT 1", "")


def test_saved_queries_refuses_past_the_size_cap(tmp_path: Path) -> None:
    saved = SavedQueries(tmp_path / "explore-queries.json")
    huge_note = "x" * saved.MAX_TOTAL_BYTES
    with pytest.raises(ValueError, match="would exceed"):
        saved.upsert("big", "SELECT 1", huge_note)
    assert saved.all() == []


def test_saved_queries_refuses_the_entry_that_crosses_the_size_cap(tmp_path: Path) -> None:
    """A file already near the cap, not one entry alone over it — the boundary
    `upsert` actually checks (total serialized size, not one field's length)."""
    saved = SavedQueries(tmp_path / "explore-queries.json")
    almost_full = "x" * (saved.MAX_TOTAL_BYTES - 200)
    queries = saved.upsert("first", "SELECT 1", almost_full)
    assert queries == [{"name": "first", "sql": "SELECT 1", "note": almost_full}]

    with pytest.raises(ValueError, match="would exceed"):
        saved.upsert("second", "SELECT 1", "x" * 500)
    assert saved.all() == queries


def test_saved_endpoint_400s_a_put_past_the_entry_cap(base_url: str, store: Path) -> None:
    path = store.parent / "explore-queries.json"
    full = [
        {"name": f"q{n:03}", "sql": "SELECT 1", "note": ""} for n in range(SavedQueries.MAX_ENTRIES)
    ]
    path.write_text(json.dumps(full))

    status, body = _send("PUT", f"{base_url}/api/saved", {"name": "one more", "sql": "SELECT 1"})
    assert status == 400
    assert "too many saved queries" in str(body["error"])
    assert json.loads(path.read_text()) == full


def test_saved_endpoint_400s_a_put_past_the_size_cap(base_url: str, store: Path) -> None:
    path = store.parent / "explore-queries.json"
    note = "x" * (SavedQueries.MAX_TOTAL_BYTES - 200)
    almost_full = [{"name": "first", "sql": "SELECT 1", "note": note}]
    path.write_text(json.dumps(almost_full))

    entry = {"name": "second", "sql": "SELECT 1", "note": "x" * 500}
    status, body = _send("PUT", f"{base_url}/api/saved", entry)
    assert status == 400
    assert "would exceed" in str(body["error"])
    assert json.loads(path.read_text()) == almost_full


def test_saved_endpoint_labels_a_corrupt_saved_file_as_saved_queries(
    base_url: str, store: Path
) -> None:
    (store.parent / "explore-queries.json").write_text("not json")

    status, body = _get(f"{base_url}/api/saved")
    assert status == 500
    assert str(json.loads(body)["error"]).startswith("saved queries: ")


def test_saved_endpoint_labels_an_unreadable_saved_file_as_saved_queries(
    base_url: str, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved_file = store.parent / "explore-queries.json"
    saved_file.write_text("[]")
    real = Path.read_text

    def read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == saved_file:
            raise PermissionError("denied")
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", read_text)

    status, body = _get(f"{base_url}/api/saved")
    assert (status, json.loads(body)["error"]) == (500, "saved queries: denied")


def test_saved_endpoint_labels_an_unwritable_saved_file_as_saved_queries(
    base_url: str, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved_file = store.parent / "explore-queries.json"
    real = Path.write_text

    def write_text(self: Path, *args: object, **kwargs: object) -> int:
        if self.parent == saved_file.parent and self.name.startswith(saved_file.name):
            raise PermissionError("read-only file system")
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", write_text)

    status, body = _send("PUT", f"{base_url}/api/saved", {"name": "heat", "sql": "SELECT 1"})
    assert (status, body["error"]) == (500, "saved queries: read-only file system")
    assert not saved_file.exists()


def test_a_store_read_oserror_is_not_labelled_as_saved_queries(
    base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(explore, "title_map", fail)

    status, body = _get(f"{base_url}/api/map?kind=movie")
    assert (status, json.loads(body)["error"]) == (500, "could not read the store: denied")


def test_scheduler_writes_saved_queries_to_their_own_mount_when_set(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production sets `PLEXDB_EXPLORE_SAVED_PATH` to a mount of its own, never
    the snapshot's directory (plex-db-ex-oyg.3)."""
    snapshot = tmp_path / "snapshot" / "plexdb.snapshot.db"
    saved_path = tmp_path / "explore-data" / "explore-queries.json"
    saved_path.parent.mkdir(parents=True)
    port = _free_port()
    monkeypatch.setenv(schedule.SCHEDULE_VAR, "03:30")
    monkeypatch.setenv(EXPLORE_PORT_VAR, str(port))
    monkeypatch.setenv(EXPLORE_SAVED_PATH_VAR, str(saved_path))
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("PLEXDB_SNAPSHOT_PATH", str(snapshot))

    schedule.run_scheduler(migrate=lambda: None, extras=lambda: None, iterations=0)
    publish(store, snapshot)

    status, _ = _send(
        "PUT", f"http://127.0.0.1:{port}/api/saved", {"name": "heat", "sql": "SELECT 1"}
    )
    assert status == 200
    assert saved_path.exists()
    assert not (snapshot.parent / "explore-queries.json").exists()


def _mock_plex(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def _poster_server(
    store: Path,
    *,
    http: httpx.Client | None = None,
    plex_url: str = "http://plex.local",
    plex_token: str = "tok",
) -> ThreadingHTTPServer:
    server = make_server(
        store, "127.0.0.1", 0, plex_url=plex_url, plex_token=plex_token, poster_http=http
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _stop(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


def test_poster_is_not_configured_without_plex_credentials(base_url: str) -> None:
    status, body = _get(f"{base_url}/api/poster?item_id=imdb:tt1")
    assert status == 503 and "not configured" in json.loads(body)["error"]


def test_poster_404s_when_no_rating_key_is_on_file(store: Path) -> None:
    server = _poster_server(store, http=_mock_plex(lambda r: httpx.Response(200, content=b"x")))
    try:
        status, body = _get(f"http://127.0.0.1:{server.server_port}/api/poster?item_id=imdb:tt1")
        assert status == 404 and "rating key" in json.loads(body)["error"]
    finally:
        _stop(server)


def test_poster_proxies_the_plex_thumb_with_the_token_kept_server_side(store: Path) -> None:
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
            "VALUES ('42', 'imdb:tt1', '1', ?)",
            (FETCHED,),
        )
        conn.commit()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers["X-Plex-Token"] == "tok"
        return httpx.Response(200, content=b"fakejpegbytes", headers={"Content-Type": "image/jpeg"})

    server = _poster_server(store, http=_mock_plex(handler))
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/poster?item_id=imdb:tt1"
        status, body = _get(url)
        assert status == 200 and body == b"fakejpegbytes"
        # A second request must not reach Plex again, or leak the token into a URL.
        status2, body2 = _get(url)
        assert status2 == 200 and body2 == b"fakejpegbytes"
        assert len(calls) == 1
        assert "tok" not in calls[0]
    finally:
        _stop(server)


def test_poster_refuses_an_oversize_response(store: Path) -> None:
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
            "VALUES ('42', 'imdb:tt1', '1', ?)",
            (FETCHED,),
        )
        conn.commit()
    oversize = b"x" * (explore.POSTER_MAX_BYTES + 1)
    server = _poster_server(store, http=_mock_plex(lambda r: httpx.Response(200, content=oversize)))
    try:
        status, body = _get(f"http://127.0.0.1:{server.server_port}/api/poster?item_id=imdb:tt1")
        assert status == 503 and "over" in json.loads(body)["error"]
    finally:
        _stop(server)


def test_query_endpoint_answers_rows_and_400s_a_refusal(base_url: str) -> None:
    url = f"{base_url}/api/query"
    status, out = _send("POST", url, {"sql": "SELECT COUNT(*) AS n FROM items"})
    assert status == 200 and out["rows"] == [[6]]
    status, out = _send("POST", url, {"sql": "DELETE FROM items"})
    assert status == 400 and "not authorized" in str(out["error"])


def test_query_endpoint_answers_429_while_both_slots_run(base_url: str) -> None:
    held = [explore.QUERY_SLOTS.acquire(blocking=False) for _ in range(2)]
    try:
        status, out = _send("POST", f"{base_url}/api/query", {"sql": "SELECT 1"})
        assert status == 429 and "already running" in str(out["error"])
    finally:
        for _ in filter(None, held):
            explore.QUERY_SLOTS.release()
    assert _send("POST", f"{base_url}/api/query", {"sql": "SELECT 1"})[0] == 200


def test_query_endpoint_refuses_an_oversize_body(base_url: str) -> None:
    # The server answers from the header alone, without reading the body. Sending
    # the header with no body keeps the client from writing into a closed socket.
    host, port = base_url.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port))
    conn.request(
        "POST",
        "/api/query",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(explore.MAX_BODY + 1),
        },
    )
    response = conn.getresponse()
    out = json.loads(response.read())
    conn.close()
    assert response.status == 400 and "body is over" in str(out["error"])


def test_query_endpoint_refuses_a_body_that_is_not_json_typed(base_url: str) -> None:
    request = urllib.request.Request(
        f"{base_url}/api/query",
        data=json.dumps({"sql": "SELECT 1"}).encode(),
        method="POST",
        headers={"Content-Type": "text/plain"},
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request)
    assert caught.value.code == 400
    assert "application/json" in json.loads(caught.value.read())["error"]


# ---- ?clip and ?demo in a headless browser ----
# The recorder contract in docs/explore.md, read off the page with Playwright. Only `superhero`
# of the three demo tags is on this store's movie map, so the period here is 2.5 s, not 4.5 s.

CLIP_READY = "() => window.__clip || document.body.dataset.demo === 'failed'"
# ?demo exposes no seam; its caption names a tag once the live tour has reached the first one.
DEMO_READY = (
    "() => document.getElementById('democap').childElementCount"
    " || document.body.dataset.demo === 'failed'"
)
CLIP_FRAME = "() => ({ k: mapVP.k, tx: mapVP.tx, ty: mapVP.ty, tag: state.tag })"
# The fit the viewport should draw at zoom 1, worked out from the canvas size and the map bounds.
CLIP_FIT = """() => {
  const c = document.getElementById("map"), b = mapVP.bounds, pad = mapVP.pad;
  return { k: mapVP.k, fit: Math.min((c.clientWidth - 2 * pad) / (b.x1 - b.x0),
                                     (c.clientHeight - 2 * pad) / (b.y1 - b.y0)) };
}"""


@pytest.fixture
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        chromium = pw.chromium.launch(headless=True)
        try:
            yield chromium
        finally:
            chromium.close()


def _open_clip(browser: Browser, store: Path, url: str, ready: str = CLIP_READY) -> Page:
    refresh_map(store)
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(url)
    page.wait_for_function(ready, timeout=60_000)
    assert page.evaluate("() => document.body.dataset.demo") != "failed", page.evaluate(
        "() => document.getElementById('error').textContent"
    )
    return page


def _seek(page: Page, t: float) -> dict[str, Any]:
    page.evaluate("t => window.__clip.seek(t)", t)
    return dict(page.evaluate(CLIP_FRAME))


def test_a_line_panel_whose_every_night_is_zero_still_draws_its_point(
    browser: Browser, base_url: str, store: Path
) -> None:
    seeded = PanelsFile(store.parent / "explore-panels.json").read_or_seed()
    damaged = seeded[0].panels[0]
    (store.parent / "panel-history.json").write_text(
        json.dumps({"series": {series_key(damaged): [{"day": "2026-10-21", "value": 0}]}})
    )
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(base_url)
    page.click('button[data-tab="query"]')
    page.wait_for_selector("circle.dot")

    assert page.get_attribute("circle.dot", "cy") != "NaN"
    assert page.locator("svg text", has_text="NaN").count() == 0


def test_the_dashboard_draws_the_four_seeded_panels_and_edit_opens_the_editor(
    browser: Browser, base_url: str
) -> None:
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(base_url)
    page.click('button[data-tab="query"]')
    page.wait_for_selector(".tile")

    assert page.locator(".tile h3").all_inner_texts() == [
        "Damaged or rule-breaking files",
        "English original, no English subtitle",
        "Decode progress",
        "Tracks per language",
    ]
    assert page.locator("#dashpick option:checked").inner_text() == "Library health"
    assert page.inner_text("#qtitle .sub") == "4 panels \u00b7 recorded nightly by the sweep"
    assert page.is_hidden("#sql")

    page.locator(".tile button", has_text="Edit").nth(2).click()

    assert page.is_visible("#sql") and page.is_hidden("#dashwrap")
    assert "parts_decoded" in page.input_value("#sql")


def test_the_dashboard_shows_a_loading_line_until_the_request_answers(
    browser: Browser, base_url: str
) -> None:
    held: dict[str, Any] = {}
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.route(lambda u: "/api/dashboards" in u, lambda r: held.update(req=r))
    page.goto(base_url)
    page.click('button[data-tab="query"]')
    page.wait_for_selector("#dashloading")

    assert page.inner_text("#dashloading") == "Loading dashboards…"
    assert page.locator(".tile").count() == 0

    held.pop("req").continue_()
    page.wait_for_selector(".tile")
    assert page.locator("#dashloading").count() == 0


_TRACKS = (
    "SELECT 'eng' AS lang, 'audio' AS kind, 10 AS n UNION ALL SELECT 'eng', 'subtitle', 9 "
    "UNION ALL SELECT 'jpn', 'audio', 4 UNION ALL SELECT 'fre', 'audio', 3 "
    "UNION ALL SELECT 'spa', 'audio', 2 UNION ALL SELECT 'ger', 'audio', 1 "
    "UNION ALL SELECT 'ita', 'audio', 1 UNION ALL SELECT 'por', 'audio', 1"
)
_TRACKS_BY_LANGUAGE = (
    f"SELECT lang, SUM(n) AS n FROM ({_TRACKS}) GROUP BY lang ORDER BY n DESC, lang"
)
_COLUMNS = {"label": "lang", "number": "n", "x": "kind", "y": "lang"}


def _chart_panels(sql: dict[str, str], value: str = "rows") -> list[dict[str, Any]]:
    return [
        {
            "id": chart,
            "title": chart,
            "chart": chart,
            "sql": sql.get(chart, sql["*"]),
            "value": value,
            "width": 1,
            "height": "short",
            **({k: v for k, v in _COLUMNS.items() if chart != "stat"}),
        }
        for chart in ("line", "stat", "bar", "pie", "table", "heatmap")
    ]


def _open_dashboard(
    browser: Browser, base_url: str, store: Path, panels: list[dict[str, Any]]
) -> Page:
    (store.parent / "explore-panels.json").write_text(
        json.dumps({"dashboards": [{"id": "d", "name": "D", "panels": panels}]})
    )
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(base_url)
    page.click('button[data-tab="query"]')
    page.wait_for_selector(".tile")
    return page


def test_a_bar_panel_caps_at_its_tiles_rows_and_sums_the_rest_into_other(
    browser: Browser, base_url: str, store: Path
) -> None:
    def bars(rows: int, height: str) -> tuple[list[str], list[str]]:
        sql = " UNION ALL ".join(f"SELECT 'l{i:02}' AS lang, {i} AS n" for i in range(1, rows + 1))
        panel = _chart_panels({"*": sql})[2] | {"height": height}
        page = _open_dashboard(browser, base_url, store, [panel])
        box = page.locator(".bars")
        out = (box.locator(".bl").all_inner_texts(), box.locator(".bv").all_inner_texts())
        page.close()
        return out

    names, values = bars(20, "tall")
    assert names == [f"l{i:02}" for i in range(20, 12, -1)] + ["other"]
    assert values[-1] == str(sum(range(1, 13)))
    names, values = bars(20, "short")
    assert names == ["l20", "l19", "l18", "l17", "other"]
    assert values[-1] == str(sum(range(1, 17)))
    names, _ = bars(3, "tall")
    assert names == ["l03", "l02", "l01"]


def test_pie_table_and_heatmap_draw_their_rows_and_stat_draws_a_sparkline(
    browser: Browser, base_url: str, store: Path
) -> None:
    panels = _chart_panels({"*": _TRACKS, "pie": _TRACKS_BY_LANGUAGE})
    path = store.parent / "explore-panels.json"
    path.write_text(json.dumps({"dashboards": [{"id": "d", "name": "D", "panels": panels}]}))
    stat = next(p for p in PanelsFile(path).read_or_seed()[0].panels if p.chart == "stat")
    (store.parent / "panel-history.json").write_text(
        json.dumps(
            {
                "series": {
                    series_key(stat): [
                        {"day": "2026-10-20", "value": 5},
                        {"day": "2026-10-21", "value": 8},
                    ]
                }
            }
        )
    )
    page = _open_dashboard(browser, base_url, store, panels)

    def tile(title: str) -> Any:
        return page.locator(".tile", has=page.locator("h3", has_text=title))

    assert tile("stat").locator("svg polyline.spark").count() == 1
    assert "+3 since Oct 20" in tile("stat").inner_text()
    legend = tile("pie").locator(".pielegend")
    assert legend.locator("span:not(.n):not(.pc)").all_inner_texts() == [
        "eng", "jpn", "fre", "spa", "ger", "other",
    ]  # fmt: skip
    assert legend.locator(".n").all_inner_texts() == ["19", "4", "3", "2", "1", "2"]
    assert legend.locator(".pc").all_inner_texts() == ["61%", "13%", "10%", "6%", "3%", "6%"]
    assert tile("pie").locator("svg path").count() == 6
    for width in (1280, 1000, 800):
        page.set_viewport_size({"width": width, "height": 800})
        pie = tile("pie").locator(".pie svg").bounding_box()
        side = legend.bounding_box()
        assert pie is not None and side is not None
        assert side["x"] >= pie["x"] + pie["width"]  # the legend sits beside the pie
        assert abs((side["y"] + side["height"] / 2) - (pie["y"] + pie["height"] / 2)) < 2
        tall = max(
            (tile(t).bounding_box() or {"height": 0})["height"] for t in ("bar", "heatmap", "table")
        )
        assert (tile("pie").bounding_box() or {"height": 999})["height"] <= tall
    page.close()

    big = (
        "SELECT 'mkv' AS lang, 777512 AS n UNION ALL SELECT 'mp4', 86690 "
        "UNION ALL SELECT 'avi', 9680 UNION ALL SELECT '(none)', 7000 "
        "UNION ALL SELECT 'mpegts', 60000 UNION ALL SELECT 'a-very-long-container-name', 5"
    )
    page = _open_dashboard(browser, base_url, store, _chart_panels({"*": big}))
    for width in (1280, 1000):
        page.set_viewport_size({"width": width, "height": 800})
        box = tile("pie").bounding_box()
        assert box is not None
        cells = (
            tile("pie")
            .locator(".pielegend > *")
            .evaluate_all(
                "els => els.map(e => { const r = e.getBoundingClientRect();"
                " return [r.right, Math.round(r.top + r.height / 2)]; })"
            )
        )
        assert len(cells) == 24
        assert all(right <= box["x"] + box["width"] for right, _ in cells)
        assert len({mid for _, mid in cells}) == 6  # a row's four cells share a line
    page.close()
    page = _open_dashboard(browser, base_url, store, panels)
    assert tile("table").locator("th").all_inner_texts() == ["lang", "kind", "n"]
    assert tile("table").locator("tbody tr").count() == 8
    heat = tile("heatmap")
    assert heat.locator(".hx").all_inner_texts() == ["audio", "subtitle"]
    assert heat.locator(".hy").all_inner_texts() == [
        "eng",
        "jpn",
        "fre",
        "spa",
        "ger",
        "ita",
        "por",
    ]
    assert heat.locator(".hc").first.inner_text() == "10"
    assert heat.locator(".hc.none").count() == 6
    assert heat.locator(".hc.hot").count() == 2


def test_a_table_panel_over_more_than_50_rows_says_how_many_it_shows(
    browser: Browser, base_url: str, store: Path
) -> None:
    def note(rows: int) -> tuple[int, str]:
        sql = (
            "WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s "
            f"WHERE n < {rows}) SELECT n AS lang, n AS kind, n FROM s"
        )
        page = _open_dashboard(browser, base_url, store, [_chart_panels({"*": sql})[4]])
        tile = page.locator(".tile")
        out = (tile.locator("tbody tr").count(), tile.locator(".tilenote").all_inner_texts())
        page.close()
        return out[0], " ".join(out[1])

    assert note(120) == (50, "First 50 of 120 rows")
    assert note(50) == (50, "")
    assert note(600) == (50, "First 50 of 500+ rows")


def test_a_panel_of_every_type_with_no_rows_says_so(
    browser: Browser, base_url: str, store: Path
) -> None:
    empty = f"SELECT * FROM ({_TRACKS}) WHERE n < 0"
    panels = _chart_panels({"*": empty}, value="column:n")
    panels.append(
        {
            "id": "gauge",
            "title": "gauge",
            "chart": "gauge",
            "sql": empty,
            "value": "column:n",
            "total": "n",
            "width": 1,
            "height": "short",
        }
    )
    page = _open_dashboard(browser, base_url, store, panels)

    assert page.locator(".tile").count() == 7
    assert page.locator(".tile .tilenote", has_text="No rows tonight.").count() == 7


def test_clip_seek_draws_the_same_frame_at_t_and_t_plus_period(
    browser: Browser, store: Path, base_url: str
) -> None:
    page = _open_clip(browser, store, f"{base_url}/?clip")
    period = page.evaluate("() => window.__clip.period")
    frames = {t: _seek(page, t) for t in (0, 0.7, 1.0, 1.9, 2.2)}
    for t, frame in frames.items():
        assert _seek(page, t + period) == frame, t
    # A tag is lit and the camera moves somewhere in the loop, so the seam compares real frames.
    assert {f["tag"] for f in frames.values()} > {None}
    assert len({f["k"] for f in frames.values()}) > 1


def test_clip_seek_after_a_resize_fits_the_whole_map_to_the_new_canvas(
    browser: Browser, store: Path, base_url: str
) -> None:
    page = _open_clip(browser, store, f"{base_url}/?clip")
    _seek(page, 1.0)  # a zoomed frame, so the viewport is no longer fitted when the resize lands
    # 900x600 seeks before the ResizeObserver can run; 1600x500 seeks after it has.
    for width, height, settle in ((900, 600, False), (1600, 500, True)):
        page.set_viewport_size({"width": width, "height": height})
        if settle:
            page.evaluate(
                "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
            )
        _seek(page, 0)
        drawn = page.evaluate(CLIP_FIT)
        assert drawn["k"] == pytest.approx(drawn["fit"], rel=1e-9), (width, height)


def test_clip_forces_the_tags_tab_over_the_hash(
    browser: Browser, store: Path, base_url: str
) -> None:
    page = _open_clip(browser, store, f"{base_url}/?clip#tab=query")
    assert page.evaluate("() => state.tab") == "tags"
    assert page.evaluate("() => document.getElementById('tagsmain').hidden") is False


def test_demo_exposes_no_clip_seam(browser: Browser, store: Path, base_url: str) -> None:
    page = _open_clip(browser, store, f"{base_url}/?demo", ready=DEMO_READY)
    assert page.evaluate("() => window.__clip") is None


def _pin_while_the_map_redraws(browser: Browser, store: Path, base_url: str, pin: str) -> Page:
    """Run `pin` on the dimmed default map, then land the redraw without the noise tags
    before the title's details: the order that swaps every point under a pin in flight."""
    refresh_map(store)
    held: dict[str, Any] = {}
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.add_init_script("localStorage.setItem('plexdb-explore:noise', '[\"stinger\"]')")
    page.route(lambda u: "/api/map?" in u and "exclude=" in u, lambda r: held.update(map=r))
    page.route(lambda u: "/api/title?" in u, lambda r: held.update(title=r))
    page.goto(f"{base_url}/#kind=movie&view=map")
    page.wait_for_function("() => map.dim && map.byId.has('imdb:tt1')", timeout=30_000)
    page.evaluate(pin)
    deadline = time.monotonic() + 10
    while "title" not in held:
        assert time.monotonic() < deadline, "the pin never asked for the title's details"
        page.wait_for_timeout(20)
    held.pop("map").continue_()
    page.wait_for_function("() => !map.dim", timeout=30_000)
    held.pop("title").continue_()
    page.wait_for_function("() => document.querySelector('#rightcol .tcard ol')", timeout=30_000)
    return page


def test_a_title_pinned_while_the_map_redraws_keeps_its_card_and_nearest_titles(
    browser: Browser, store: Path, base_url: str
) -> None:
    page = _pin_while_the_map_redraws(
        browser, store, base_url, "() => mapVP.onSelect(map.byId.get('imdb:tt1'))"
    )
    pin = page.evaluate(
        """() => ({ id: mapVP.selected && mapVP.selected.id,
                    onMap: mapVP.selected === map.byId.get('imdb:tt1'),
                    open: !pinCard.root.hidden, similar: mapUI.similar })"""
    )
    assert pin["id"] == "imdb:tt1" and pin["onMap"] and pin["open"], pin
    assert "imdb:tt2" in pin["similar"], pin


def test_a_title_searched_while_the_map_redraws_pins_its_point_on_the_new_map(
    browser: Browser, store: Path, base_url: str
) -> None:
    page = _pin_while_the_map_redraws(
        browser,
        store,
        base_url,
        "() => { chooseSuggestion(suggestions('iron man')[0]); }",
    )
    page.wait_for_function("() => mapUI.search && mapVP.selected", timeout=10_000)
    pin = page.evaluate(
        """() => ({ id: mapVP.selected.id, onMap: mapVP.selected === map.byId.get('imdb:tt1'),
                    focus: mapUI.search.focus })"""
    )
    assert pin == {"id": "imdb:tt1", "onMap": True, "focus": "imdb:tt1"}, pin


def test_a_redraw_under_a_zoomed_pin_keeps_the_zoom_and_centres_the_pin_beside_its_card(
    browser: Browser, store: Path, base_url: str
) -> None:
    page = _pin_while_the_map_redraws(
        browser,
        store,
        base_url,
        "() => { mapVP.onSelect(map.byId.get('imdb:tt1')); mapVP.zoomAt(0, 0, 8); }",
    )
    view = page.evaluate(
        """() => { const it = mapVP.selected, pad = clearOfPinCard();
                   return { zoom: mapVP.zoom, fitted: mapVP.fitted,
                            x: mapVP.sx(it.x), y: mapVP.sy(it.y),
                            cx: (pad.l + mapVP.w - pad.r) / 2,
                            cy: (pad.t + mapVP.h - pad.b) / 2 }; }"""
    )
    assert view["zoom"] == pytest.approx(8) and not view["fitted"], view
    assert (view["x"], view["y"]) == pytest.approx((view["cx"], view["cy"]), abs=1), view


def test_merges_jev_only_filter_lists_jev_calls_and_no_rule_calls(
    browser: Browser, store: Path, base_url: str
) -> None:
    with open_store(store) as conn:
        conn.executemany(
            "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_score, jev_model, judged_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("stinger", "superhero", 0.95, "jev-1.13.0", FETCHED),
                ("heist", "lawyer", 1.0, "rule:film-suffix", FETCHED),
            ],
        )
        conn.commit()
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(f"{base_url}/#tab=merges")
    page.wait_for_function(
        "() => document.querySelectorAll('#mrows li .badge').length", timeout=30_000
    )
    badges = "els => els.map(e => e.textContent)"
    every = page.eval_on_selector_all("#mrows li .badge", badges)
    assert sorted(every) == ["Jev ≥ 0.9", "film-suffix rule"], every
    page.click("button[data-mshow='jev']")
    jev = page.eval_on_selector_all("#mrows li .badge", badges)
    assert jev == ["Jev ≥ 0.9"], jev


def test_demo_moves_the_camera_without_a_seek(browser: Browser, store: Path, base_url: str) -> None:
    page = _open_clip(browser, store, f"{base_url}/?demo", ready=DEMO_READY)
    # One second of samples, longer than the 0.6 s the camera holds still on a tag.
    xs = page.evaluate(
        """() => new Promise(r => {
          const xs = [], t0 = performance.now();
          const take = () => { xs.push(mapVP.tx);
                               performance.now() - t0 < 1000 ? setTimeout(take, 50) : r(xs); };
          take();
        })"""
    )
    assert len(set(xs)) > 1, xs


# (shared, N, df_anchor, df_other, z) -> cautious lift, for heist on the 2026-10 movie store
# (184 of 11,533 titles): jewelry heist (8 shared, on 9), a 2-title pair (on 2), and heist and
# thrilling action (92 shared, on 297).
CAUTIOUS_CASES = [
    ((8, 11533, 184, 9, 0.0), 8 * 11533 / (184 * 9)),
    ((8, 11533, 184, 9, 2.0), 28.063),
    ((2, 11533, 184, 2, 2.0), 16.823),
    ((2, 11533, 184, 2, 4.0), 6.338),
    ((92, 11533, 184, 297, 4.0), 13.924),
]


def _add_superhero_film(store: Path) -> None:
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt5', 'movie', 'Hancock')"
        )
        for keyword in ("superhero", "stinger"):
            conn.execute(
                "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
                "VALUES ('imdb:tt5', 'keywords', 'tmdb', 'keyword', ?, ?)",
                (keyword, FETCHED),
            )
        conn.commit()


def test_every_answer_carries_the_snapshot_it_was_built_from(store: Path, base_url: str) -> None:
    before = json.loads(_get(f"{base_url}/api/tags?kind=movie")[1])["stamp"]
    co = json.loads(_get(f"{base_url}/api/cotags?kind=movie&tag=superhero")[1])
    net = _last_ndjson(_get(f"{base_url}/api/tagnetwork?kind=movie")[1])
    assert co["stamp"] == net["stamp"] == before
    stat = store.stat()
    assert before == f"{stat.st_mtime_ns}:{stat.st_size}"

    _add_superhero_film(store)
    after = json.loads(_get(f"{base_url}/api/tags?kind=movie")[1])
    assert after["stamp"] != before and after["titles"] == 5


def test_the_page_refetches_tags_when_cotags_come_from_a_newer_snapshot(
    browser: Browser, store: Path, base_url: str
) -> None:
    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_function("() => state.data && state.data.stamp")
    old = page.evaluate("state.data.stamp")
    assert page.evaluate("state.data.titles") == 4

    # The snapshot is replaced between the page's /api/tags and its /api/cotags.
    _add_superhero_film(store)
    page.evaluate("state.tag = 'superhero'; loadCotags('superhero')")
    page.wait_for_function("old => state.data.stamp !== old", arg=old)
    page.wait_for_function(
        "() => cotags.data && cotags.data.stamp === state.data.stamp && coLive() !== null"
    )
    assert page.evaluate("state.data.titles") == 5
    assert page.evaluate("cotags.data.titles") == 5
    page.close()


def test_cautious_lift_marks_down_few_titles_and_the_page_agrees(
    browser: Browser, base_url: str
) -> None:
    """At z = 0 it is the plain lift; more evidence shrinks a 2-title lift far more than an
    8- or 92-title one. The page's evidence slider computes the same function in
    `cautiousLift`, so the panel's table and the server's chips can't drift apart."""
    for args, expected in CAUTIOUS_CASES:
        assert explore.cautious_lift(*args) == pytest.approx(expected, abs=1e-3), args
    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_function("() => typeof cautiousLift === 'function'")
    for args, _ in CAUTIOUS_CASES:
        js = page.evaluate("a => cautiousLift(...a)", list(args))
        assert js == pytest.approx(explore.cautious_lift(*args), rel=1e-12), args


def _related_note(page: Page, df: int, co: list[list[object]]) -> str | None:
    """The caveat text the Related tags tab shows for a tag on `df` titles whose co-occurrence
    is `co` (`[tag, shared, df]` rows), or None when it shows none."""
    return page.evaluate(
        """([df, co]) => {
          showTag("probe");
          Object.assign(cotags, { tag: "probe", kind: state.kind, token: cotags.token + 1,
            data: { tag: "probe", df, titles: 1000, co, stamp: state.data && state.data.stamp } });
          paintCotags();
          const n = document.querySelector("#cobox .cothin");
          return n ? n.textContent : null;
        }""",
        [df, co],
    )


def test_related_tags_say_when_the_top_pairings_rest_on_a_few_titles(
    browser: Browser, base_url: str
) -> None:
    """A tag on 31 titles whose best pairings share 1-2 titles gets a caveat naming the count;
    a tag whose best pairings share 10 or more gets none."""
    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_function("() => typeof showTag === 'function'")
    small = [[f"rare{i}", 1 + i % 2, 1 + i % 2] for i in range(12)]
    note = _related_note(page, 31, small)
    assert note is not None
    assert "1\u20132" in note and "(probe is on 31" in note, note
    big = [[f"solid{i}", 10 + i, 12 + i] for i in range(12)]
    assert _related_note(page, 184, big) is None
    # The rule's edges: shared 3 is thin, shared 4 is not.
    assert _related_note(page, 40, [[f"t{i}", 3, 3] for i in range(12)]) is not None
    assert _related_note(page, 40, [[f"t{i}", 4, 4] for i in range(12)]) is None


# ---- the Lists tab ----


def _add_lists(store: Path) -> None:
    """Two lists: one MDBList list holding Iron Man (owned) and a film the library lacks, and
    an empty Letterboxd one."""
    with open_store(store) as conn:
        conn.executemany(
            "INSERT INTO collection (collection_id, source, name, url, size, likes, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "mdblist:1",
                    "mdblist",
                    "Mindfuck Movies",
                    "https://mdblist.com/lists/lina/mf",
                    5,
                    9,
                    FETCHED,
                ),
                (
                    "letterboxd:2",
                    "letterboxd",
                    "Heist Night",
                    "https://letterboxd.com/x/list/h/",
                    2,
                    3,
                    FETCHED,
                ),
            ],
        )
        conn.execute(
            "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
            "VALUES ('imdb:tt1', 'imdb', 'tt1', 'movie', ?)",
            (FETCHED,),
        )
        conn.executemany(
            "INSERT INTO collection_entry (collection_id, rank, ns, value, kind, title, year) "
            "VALUES ('mdblist:1', ?, 'imdb', ?, 'movie', ?, ?)",
            [(1, "tt1", "Iron Man", 2008), (2, "tt99", "Primer", 2004)],
        )
        conn.commit()


def test_lists_routes_answer_the_table_and_one_lists_detail(store: Path, base_url: str) -> None:
    _add_lists(store)
    status, table = _send("GET", f"{base_url}/api/lists")
    assert status == 200
    rows = cast(list[dict[str, Any]], table["lists"])
    assert {r["collection_id"]: (r["owned"], r["entry_count"]) for r in rows} == {
        "mdblist:1": (1, 2),
        "letterboxd:2": (0, 0),
    }
    status, only = _send("GET", f"{base_url}/api/lists?source=letterboxd")
    assert [r["collection_id"] for r in only["lists"]] == ["letterboxd:2"]  # type: ignore[attr-defined]
    status, detail = _send("GET", f"{base_url}/api/list?ref=mdblist:1")
    assert status == 200
    entries = cast(list[dict[str, Any]], detail["entries"])
    assert [(e["title"], e["owned"]) for e in entries] == [("Iron Man", True), ("Primer", False)]
    status, _ = _send("GET", f"{base_url}/api/list?ref=nope")
    assert status == 404
    status, _ = _send("GET", f"{base_url}/api/list")
    assert status == 400


def test_lists_tab_selects_clears_and_makes_no_outside_request(
    browser: Browser, store: Path, base_url: str
) -> None:
    _add_lists(store)
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    outside: list[str] = []
    page.on(
        "request",
        lambda r: (
            outside.append(r.url)
            if not r.url.startswith(base_url) and not r.url.startswith("data:")
            else None
        ),
    )
    page.goto(f"{base_url}/#tab=lists")
    page.wait_for_selector("#ltable tbody tr")
    assert page.locator("#ltable tbody tr").count() == 2
    page.locator("#ltable tbody tr", has_text="Mindfuck Movies").click()
    page.wait_for_selector("#linspect .lentries li")
    assert page.locator("#linspect .lentries li").all_text_contents() == [
        "1Iron Man (2008)",
        "2Primer (2004)",
    ]
    assert page.locator("#linspect .lentries li.unowned").count() == 1
    page.locator("#ltable tbody tr", has_text="Mindfuck Movies").click()
    assert page.locator("#ltable tr.selected").count() == 0
    page.locator("#ltable tbody tr", has_text="Mindfuck Movies").click()
    page.wait_for_selector("#linspect .lentries li")
    srcs = page.eval_on_selector_all("#ltable img", "els => els.map(e => e.src.slice(0, 22))")
    assert srcs == ["data:image/png;base64,"] * 2, srcs
    page.keyboard.press("Escape")
    assert page.locator("#ltable tr.selected").count() == 0
    assert "Select a list" in page.locator("#linspect").inner_text()
    page.set_viewport_size({"width": 720, "height": 800})
    page.locator("#ltable tbody tr", has_text="Mindfuck Movies").click()
    page.wait_for_selector("#lback")
    assert not page.locator("#ltable").is_visible()
    page.click("#lback")
    assert page.locator("#ltable").is_visible()
    assert outside == []
    page.close()


def test_lists_tab_names_the_empty_state(browser: Browser, store: Path, base_url: str) -> None:
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(f"{base_url}/#tab=lists")
    page.wait_for_selector("#lempty:not([hidden])")
    assert "No lists stored yet" in page.locator("#lempty").inner_text()
    page.close()


# ---- curating lists (plex-db-ex-k1q.5) ----


def test_list_decisions_route_records_a_decision_and_the_table_shows_it_pending(
    store: Path, base_url: str
) -> None:
    _add_lists(store)
    url = "https://mdblist.com/lists/lina/mf"
    status, out = _send(
        "POST", f"{base_url}/api/list-decisions", {"url": url, "decision": "refetch"}
    )
    assert status == 200 and out["entry"]["decision"] == "refetch"  # type: ignore[index]
    status, out = _send(
        "POST",
        f"{base_url}/api/list-decisions",
        {"url": "https://www.mdblist.com/lists/Pierce/New/", "decision": "added"},
    )
    assert status == 200
    _, table = _send("GET", f"{base_url}/api/lists")
    rows = {r["url"]: r for r in cast(list[dict[str, Any]], table["lists"])}
    assert rows[url]["pending"] == "Waiting for sweep"
    new = rows["https://mdblist.com/lists/pierce/new"]
    assert (new["collection_id"], new["state"], new["pending"]) == (
        None,
        "waiting",
        "Waiting for sweep",
    )


def test_list_decisions_route_refuses_what_it_cannot_apply(store: Path, base_url: str) -> None:
    _add_lists(store)
    for body, expected in [
        ({"url": "https://example.com/x", "decision": "added"}, 400),
        ({"url": "https://mdblist.com/lists/lina/mf", "decision": "deleted"}, 400),
        ({"url": "https://mdblist.com/lists/nobody/none", "decision": "retry"}, 404),
    ]:
        status, _ = _send("POST", f"{base_url}/api/list-decisions", body)
        assert status == expected, body


def test_lists_tab_adds_excludes_and_shows_a_failed_list(
    browser: Browser, store: Path, base_url: str
) -> None:
    _add_lists(store)
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO list_attempt (list_key, attempts, attempted_at, reason) VALUES "
            "('https://mdblist.com/lists/pierce/gone', 3, '2026-10-14T03:30:00+00:00', "
            "'MDBList returned 404')"
        )
        conn.execute(
            "INSERT INTO list_decision (url, decision, decided_at, added) VALUES "
            "('https://mdblist.com/lists/pierce/gone', 'added', '2026-10-13T00:00:00+00:00', 1)"
        )
        conn.commit()
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(f"{base_url}/#tab=lists")
    page.wait_for_selector("#ltable tbody tr")

    page.fill("#laddurl", "https://mdblist.com/lists/pierce/new")
    page.click("#ladd")
    # The page clears the box and writes the status only after it has repainted the table, and
    # the failed list is already waiting, so wait on that signal rather than on any waiting row.
    page.wait_for_function("document.querySelector('#laddurl').value === ''")
    assert "The next sweep fetches it" in page.locator("#laddstatus").inner_text()
    added = page.locator("#ltable tr.waiting", has_text="pierce/new")
    assert "Waiting for sweep" in added.inner_text()

    page.fill("#laddurl", "https://example.com/x")
    page.click("#ladd")
    page.wait_for_function("document.querySelector('#laddstatus').classList.contains('bad')")

    page.locator("#ltable tbody tr", has_text="pierce/gone").click()
    inspector = page.locator("#linspect")
    inspector.get_by_text("Fetch failed 3 times in a row").wait_for()
    assert "MDBList returned 404" in inspector.inner_text()
    assert "stopped trying until you retry" in inspector.inner_text()
    assert inspector.get_by_role("button").all_text_contents().count("Retry") == 1
    inspector.get_by_role("button", name="Retry").click()
    page.wait_for_selector("#linspect .lpending")
    assert "Waiting for sweep" in inspector.inner_text()

    page.locator("#ltable tbody tr", has_text="Mindfuck Movies").click()
    page.wait_for_selector("#linspect .lentries li")
    inspector.get_by_role("button", name="Exclude").click()
    page.wait_for_selector("#linspect .lpending")
    assert "Excluded · next sweep removes it" in inspector.inner_text()
    assert inspector.get_by_role("button", name="Retry").count() == 0
    page.close()
