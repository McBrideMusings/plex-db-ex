"""The tag explorer reports keyword counts the way a keyword-cosine scorer sees them.

Driven through `build_index`, `titles_tagged` and the HTTP server a browser
talks to, against a small store whose counts can be worked out by hand.
"""

from __future__ import annotations

import json
import math
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from plexdb import explore, schedule
from plexdb.errors import ConfigError
from plexdb.explore import (
    EXPLORE_PORT_VAR,
    RECIPES,
    QueryError,
    build_index,
    make_server,
    neighbourhood,
    run_query,
    title_map,
    titles_tagged,
)
from plexdb.store import init, open_readonly, open_store, publish
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
                    "INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) "
                    "VALUES (?, 'tmdb_keywords', 'keyword', ?, ?)",
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


def test_neighbourhood_ranks_co_tags_and_links_them_by_shared_titles(store: Path) -> None:
    with open_readonly(store) as conn:
        hood = neighbourhood(conn, "movie", "superhero")
        without = neighbourhood(conn, "movie", "superhero", exclude=frozenset({"stinger"}))

    assert hood.nodes == (("superhero", 3, 3), ("stinger", 2, 2), ("based on comic", 1, 1))
    assert hood.edges == (
        ("stinger", "superhero", 2),
        ("based on comic", "stinger", 1),
        ("based on comic", "superhero", 1),
    )
    # A show's keyword never joins a movie graph.
    assert "lawyer" not in {value for value, _, _ in hood.nodes}
    assert without.nodes == (("superhero", 3, 3), ("based on comic", 1, 1))
    assert without.edges == (("based on comic", "superhero", 1),)


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
                        "INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) "
                        "VALUES (?, 'tmdb_keywords', 'keyword', ?, ?)",
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


def test_titles_tagged_lists_one_media_type_by_title(store: Path) -> None:
    with open_readonly(store) as conn:
        titles = titles_tagged(conn, "movie", "superhero")

    assert [(t.title, t.year, t.keywords) for t in titles] == [
        ("Iron Man", 2008, 3),
        ("Thor", 2011, 2),
        ("Unbreakable", 2000, 1),
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

    status, body = _get(f"{base_url}/api/graph?kind=movie&tag=superhero&exclude=stinger")
    graph = json.loads(body)
    assert status == 200 and [n["value"] for n in graph["nodes"]] == ["superhero", "based on comic"]
    assert graph["edges"] == [["based on comic", "superhero", 1]]

    status, body = _get(f"{base_url}/api/map?kind=movie")
    tmap = json.loads(body)
    assert status == 200 and len(tmap["points"]) + tmap["unplaced"] == 4

    status, body = _get(f"{base_url}/api/title?item_id=imdb:tt1")
    assert json.loads(body)["keywords"] == ["based on comic", "stinger", "superhero"]


def _add_keyword(store: Path, item_id: str, keyword: str) -> None:
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) "
            "VALUES (?, 'tmdb_keywords', 'keyword', ?, '2026-09-01T00:00:00+00:00')",
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
    assert _get(f"{base_url}/api/graph?kind=movie&tag=superhero&size=0")[0] == 400
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
    request = urllib.request.Request(url, data=data, method=method)
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


def test_query_stops_a_runaway_recursive_cte(store: Path) -> None:
    runaway = (
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT COUNT(*) FROM n"
    )
    with pytest.raises(QueryError, match="stopped after"):
        run_query(store, runaway, seconds=0.2)


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


def test_query_endpoint_answers_rows_and_400s_a_refusal(base_url: str) -> None:
    url = f"{base_url}/api/query"
    status, out = _send("POST", url, {"sql": "SELECT COUNT(*) AS n FROM items"})
    assert status == 200 and out["rows"] == [[6]]
    status, out = _send("POST", url, {"sql": "DELETE FROM items"})
    assert status == 400 and "not authorized" in str(out["error"])


def test_query_endpoint_refuses_an_oversize_body(base_url: str) -> None:
    padding = "x" * (explore.MAX_BODY + 1)
    status, out = _send("POST", f"{base_url}/api/query", {"sql": f"SELECT '{padding}'"})
    assert status == 400 and "body is over" in str(out["error"])
