"""The tag explorer reports keyword counts the way a keyword-cosine scorer sees them.

Driven through `build_index`, `titles_tagged` and the HTTP server a browser
talks to, against a small store whose counts can be worked out by hand.
"""

from __future__ import annotations

import http.client
import json
import math
import socket
import sqlite3
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from plexdb import explore, schedule
from plexdb.errors import ConfigError, StoreError
from plexdb.explore import (
    EXPLORE_PORT_VAR,
    EXPLORE_SAVED_PATH_VAR,
    RECIPES,
    QueryError,
    SavedQueries,
    build_index,
    make_server,
    region_labels,
    run_query,
    tag_network,
    title_keywords_json,
    title_map,
    titles_tagged,
)
from plexdb.store import init, open_readonly, open_store, publish
from plexdb.tagnetwork import refresh_tag_networks
from plexdb.titlemap import refresh_title_maps

FETCHED = "2026-08-09T12:00:00+00:00"

# Four movies and a show. `stinger` rides along with `superhero` on two of the
# three superhero films, which is the shape the explorer exists to expose.
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
        for item_id, (kind, title, year, keywords) in TITLES.items():
            conn.execute(
                "INSERT INTO items (item_id, type, title, year) VALUES (?, ?, ?, ?)",
                (item_id, kind, title, year),
            )
            for keyword in keywords:
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
    assert by_value["stinger"].co_tags == (("superhero", 2), ("based on comic", 1))
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


def test_region_labels_name_each_region_by_its_own_tag_and_skip_noise(tmp_path: Path) -> None:
    """Forty titles on two islands, one per genre, each tagged with its genre's
    stemmed word plus a packaging tag: the labels name the islands, show the
    readable spelling, and never use the excluded tag."""
    path = tmp_path / "plexdb.db"
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

    with open_readonly(path) as conn:
        labels = region_labels(conn, "movie", points, frozenset({"stinger"}))
        with_noise = region_labels(conn, "movie", points)

    assert {label.tag for label in labels} == {"outlaw", "galaxi"}
    assert {label.text for label in labels} == {"outlaw", "galaxies"}
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
        keywords = title_keywords_json(conn, "imdb:tt1")

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
    assert status == 200 and b"Tag explorer" in page

    status, body = _get(f"{base_url}/api/tags?kind=movie")
    tags = json.loads(body)
    assert status == 200 and tags["titles"] == 4
    assert tags["tags"][1] == {
        "value": "stinger",
        "df": 2,
        "idf": round(1 + math.log(2), 4),
        "co": [["superhero", 2], ["based on comic", 1]],
    }

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


def test_title_endpoint_returns_the_card_with_similar_titles_in_rank_order(
    base_url: str, store: Path
) -> None:
    with open_store(store) as conn:
        conn.execute(
            "UPDATE items SET studio = 'Marvel', content_rating = 'PG-13' "
            "WHERE item_id = 'imdb:tt1'"
        )
        for to_id, rank in (("imdb:tt3", 5), ("imdb:tt2", 1), ("imdb:tt4", 3)):
            conn.execute(
                "INSERT INTO edges (from_id, to_id, edge_type, rank, fetched_at) "
                "VALUES ('imdb:tt1', ?, 'tmdb_similar', ?, ?)",
                (to_id, rank, FETCHED),
            )
        # Another edge type from the same title is not a similar title.
        conn.execute(
            "INSERT INTO edges (from_id, to_id, edge_type, rank, fetched_at) "
            "VALUES ('imdb:tt1', 'tvdb:9', 'tmdb_recommendations', 0, ?)",
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
    assert [s["title"] for s in card["similar"]] == ["Thor", "Heat", "Unbreakable"]
    assert card["similar"][0] == {"item_id": "imdb:tt2", "title": "Thor", "year": 2011, "rank": 1}


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


def test_refresh_stores_both_kinds_and_skips_a_kind_whose_keywords_are_unchanged(
    store: Path,
) -> None:
    with open_store(store) as conn:
        first = refresh_title_maps(conn)
        counts = conn.execute("SELECT kind, COUNT(*) FROM title_map GROUP BY kind")
        rows = [tuple(r) for r in counts]
        again = refresh_title_maps(conn)

    assert [(r.kind, r.redrawn) for r in first] == [("movie", True), ("show", True)]
    # Heat's only tag is on no other movie, so it is left off; so is the one show.
    assert rows == [("movie", 3)]
    assert [(r.kind, r.redrawn, r.placed) for r in again] == [
        ("movie", False, 3),
        ("show", False, 0),
    ]

    _add_keyword(store, "tvdb:9", "vigilante")
    with open_store(store) as conn:
        changed = refresh_title_maps(conn)
    assert [(r.kind, r.redrawn) for r in changed] == [("movie", False), ("show", True)]


def test_map_endpoint_serves_the_stored_map_and_draws_live_once_it_is_stale(
    store: Path, base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_store(store) as conn:
        refresh_title_maps(conn)
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
    with open_store(store) as conn:
        first = refresh_tag_networks(conn)
        counts = conn.execute("SELECT kind, COUNT(*) FROM tag_network GROUP BY kind")
        rows = [tuple(r) for r in counts]
        again = refresh_tag_networks(conn)

    assert [(r.kind, r.redrawn) for r in first] == [("movie", True), ("show", True)]
    # Only "superhero" (df=3) clears the default df >= 3 floor among the movie tags.
    assert rows == [("movie", 1)]
    assert [(r.kind, r.redrawn, r.nodes) for r in again] == [
        ("movie", False, 1),
        ("show", False, 0),
    ]

    _add_keyword(store, "tvdb:9", "vigilante")
    with open_store(store) as conn:
        changed = refresh_tag_networks(conn)
    assert [(r.kind, r.redrawn) for r in changed] == [("movie", False), ("show", True)]


def test_tagnetwork_endpoint_serves_the_stored_network_and_draws_live_once_it_is_stale(
    store: Path, base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_store(store) as conn:
        refresh_tag_networks(conn)
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


def test_unmapped_stream_failure_is_logged_to_stderr(
    base_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("layout exploded")

    monkeypatch.setattr(explore, "tag_network_streaming", fail)

    _, body = _get(f"{base_url}/api/tagnetwork?kind=movie")

    assert _last_ndjson(body) == {"stage": "error", "error": "layout exploded"}
    assert "RuntimeError: layout exploded" in capsys.readouterr().err


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
    """On the host the explorer runs inside `plexdb schedule` and reads the
    snapshot. Before the first sweep publishes one it answers 503, not a
    traceback; after, it serves the snapshot's numbers without a restart."""
    snapshot = tmp_path / "snapshot" / "plexdb.snapshot.db"
    port = _free_port()
    monkeypatch.setenv(schedule.SCHEDULE_VAR, "03:30")
    monkeypatch.setenv(EXPLORE_PORT_VAR, str(port))
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("PLEXDB_SNAPSHOT_PATH", str(snapshot))

    schedule.run_scheduler(migrate=lambda: None, iterations=0)

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
        schedule.run_scheduler(migrate=lambda: None, iterations=0)


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
        with pytest.raises(QueryError, match="already running"):
            run_query(store, "SELECT 1")
    finally:
        for _ in filter(None, held):
            explore.QUERY_SLOTS.release()
    assert run_query(store, "SELECT 1")["row_count"] == 1


def test_every_recipe_runs_against_the_schema(store: Path) -> None:
    for recipe in RECIPES:
        run_query(store, recipe["sql"])


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

    schedule.run_scheduler(migrate=lambda: None, iterations=0)
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
