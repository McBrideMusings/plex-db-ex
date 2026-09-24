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
    build_index,
    make_server,
    neighbourhood,
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


def test_index_cache_recomputes_when_the_store_changes_and_drops_key_locks(
    store: Path,
) -> None:
    tags = explore._IndexCache(store)
    before = tags.get("show")
    assert tags.get("show") is before
    assert tags._cache._locks == {}

    _add_keyword(store, "tvdb:9", "vigilante")
    after = tags.get("show")
    assert after is not before
    assert "vigilante" in [t["value"] for t in after["tags"]]  # type: ignore[attr-defined]


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
