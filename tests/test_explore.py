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

from plexdb import schedule
from plexdb.errors import ConfigError
from plexdb.explore import EXPLORE_PORT_VAR, build_index, make_server, titles_tagged
from plexdb.store import init, open_readonly, open_store, publish

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


def test_server_refuses_what_it_cannot_answer(base_url: str) -> None:
    status, body = _get(f"{base_url}/api/tags?kind=episode")
    assert status == 400 and "episode" in json.loads(body)["error"]
    assert _get(f"{base_url}/api/titles?kind=movie")[0] == 400
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
