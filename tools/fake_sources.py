"""A recorded AniList, Wikidata, MDBList (ratings) and Letterboxd (film pages) for
`tools/parity.py`.

The four enrichers read services whose answers change between two runs and whose quota a real
run spends. To compare the Python and Rust sweeps, both are pointed at this one local server,
which answers the same bytes to each. Every answer is a pure function of the request, and the
Fribb mapping and the ids are built from the store being compared, so the titles the sweeps
match, skip, fail on and write twice are decided by the store's own rows.

The server logs every request (method, path and the ids or query it carried, minus the key) so
a run's requests can be compared as well as its rows.

Environment, read when the server starts:

- `PARITY_FAKE_QUOTA_AFTER=N` answers MDBList's batch endpoint with 429 after N requests.
- `PARITY_FAKE_LB_BROKEN=1` serves a third of the film pages without their film marker.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import threading
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

API_KEY = "parity-key"
#: The AniList id whose batch the server answers 500, and the IMDb id whose Wikidata and MDBList
#: batches it answers 500.
FAIL_ANILIST_ID = 4_000_001
_ROWS = 40


@dataclass
class Scenario:
    mapping: list[Any] = field(default_factory=list)
    failing_imdb: str = ""
    requests: list[str] = field(default_factory=list)
    quota_after: int | None = None
    ratings_calls: int = 0
    lb_broken: bool = False

    def seed(self, conn: sqlite3.Connection) -> None:
        """Nothing is seeded: the baseline's own cursors decide what is stale. Each side starts
        with the quota unspent."""
        self.ratings_calls = 0


def _seed(*parts: object) -> int:
    return zlib.crc32("|".join(map(str, parts)).encode())


# --- AniList -------------------------------------------------------------------------------

_TAGS = [
    "Heist",
    "heists",
    "Time Travel",
    "Time-Travel",
    "Magic",
    "Isekai",
    "Post-Apocalyptic",
    "Coming of Age",
    "Military",
    "Cyberpunk",
    "Found Family",
    "Death Game",
    "Survival",
    "Women's Rights",
]
_CATEGORIES = [
    "Theme-Fantasy",
    "Theme-Other-Organisations",
    "Setting-Time-Present",
    "Setting-Scene",
    "Cast-Traits",
    "Cast-Traits-Species",
    "Demographic",
    None,
    5,
]


def _anilist_media(anilist_id: int) -> dict[str, Any] | None:
    seed = _seed("anilist", anilist_id)
    if anilist_id % 7 == 3:
        return None
    tags: list[dict[str, Any]] = []
    for n in range(seed % 6):
        pick = _seed("tag", anilist_id, n)
        tag: dict[str, Any] = {
            "name": _TAGS[pick % len(_TAGS)],
            "rank": [pick % 101, None, "bad", 100, 0][(pick >> 8) % 5],
            "category": _CATEGORIES[(pick >> 12) % len(_CATEGORIES)],
            "isMediaSpoiler": bool((pick >> 4) % 4 == 0),
            "isGeneralSpoiler": bool((pick >> 6) % 5 == 0),
        }
        tags.append(tag)
    if seed % 5 == 0:
        tags.append({"name": "  ", "rank": 50, "category": "Theme", "isMediaSpoiler": False})
    if seed % 4 == 0 and tags:
        tags.append(dict(tags[0], rank=(seed % 90) + 5))
    return {"id": anilist_id, "tags": tags if seed % 9 else None}


def _mapping(store: Path) -> list[Any]:
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    def rows(kind: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT i.item_id, "
            "(SELECT MIN(e.value) FROM external_ids e WHERE e.item_id = i.item_id "
            "  AND e.ns = 'tmdb') AS tmdb, "
            "(SELECT MIN(e.value) FROM external_ids e WHERE e.item_id = i.item_id "
            "  AND e.ns = 'imdb') AS imdb, "
            "(SELECT MIN(e.value) FROM external_ids e WHERE e.item_id = i.item_id "
            "  AND e.ns = 'tvdb') AS tvdb "
            "FROM items i WHERE i.type = ? ORDER BY i.item_id",
            (kind,),
        ).fetchall()

    shows = [r for r in rows("show") if r["tmdb"] and r["tmdb"].isdigit()][:_ROWS]
    movies = [r for r in rows("movie") if r["tmdb"] and r["tmdb"].isdigit()][:_ROWS]
    shows_imdb = [r for r in rows("show") if r["imdb"]][_ROWS : _ROWS + 10]
    shows_tvdb = [r for r in rows("show") if r["tvdb"] and r["tvdb"].isdigit()][_ROWS + 10 :][:10]
    movies_imdb = [r for r in rows("movie") if r["imdb"]][_ROWS : _ROWS + 10]
    conn.close()

    entries: list[Any] = []
    next_id = 1000
    for index, row in enumerate(shows):
        tmdb = int(row["tmdb"])
        form = index % 3
        entry: dict[str, Any] = {"anilist_id": next_id}
        if form == 0:
            entry["themoviedb_id"] = {"tv": tmdb}
        elif form == 1:
            entry["themoviedb_id"] = tmdb
            entry["type"] = "TV"
        else:
            entry["themoviedb_id"] = [tmdb, 999_999_999]
            entry["type"] = "OVA"
        entries.append(entry)
        next_id += 1
        if index % 4 == 0:  # a second season of the same show
            entries.append({"anilist_id": next_id, "themoviedb_id": {"tv": [tmdb]}})
            next_id += 1
    for index, row in enumerate(movies):
        tmdb = int(row["tmdb"])
        if index % 2 == 0:
            entries.append({"anilist_id": next_id, "themoviedb_id": {"movie": tmdb}})
        else:
            entries.append({"anilist_id": next_id, "themoviedb_id": str(tmdb), "type": "MOVIE"})
        next_id += 1
    for row in shows_imdb:
        entries.append({"anilist_id": next_id, "imdb_id": row["imdb"], "themoviedb_id": None})
        next_id += 1
    for row in movies_imdb:
        entries.append({"anilist_id": next_id, "imdb_id": [row["imdb"]], "type": "MOVIE"})
        next_id += 1
    for row in shows_tvdb:
        entries.append({"anilist_id": next_id, "tvdb_id": int(row["tvdb"])})
        next_id += 1
    # A movie placed under a show type is not found as a movie; a movie's tvdb id is never used.
    if movies:
        entries.append({"anilist_id": next_id, "themoviedb_id": int(movies[0]["tmdb"])})
        next_id += 1
    entries += [
        {"themoviedb_id": 5},
        {"anilist_id": True, "themoviedb_id": 6},
        {"anilist_id": "7", "themoviedb_id": 7},
        "junk",
        {"anilist_id": 9_000_000, "themoviedb_id": {"tv": 999_999_999, "other": 1}},
    ]
    if shows:
        entries.append(
            {"anilist_id": FAIL_ANILIST_ID, "themoviedb_id": {"tv": int(shows[-1]["tmdb"])}}
        )
    return entries


# --- Wikidata ------------------------------------------------------------------------------

_PLACES = ["Paris", "New York City", "paris", " Tokyo ", "Los Angeles"]
_PERIODS = ["1920s", "Victorian era", "Cold War"]
_SUBJECTS = ["heist", "Heist", "time travel", "friendship"]
_GENRES = ["heist film", "drama film", "science fiction film", "Heist Film"]
_AWARDS = ["Academy Award for Best Sound", "Palme d'Or", "Golden Globe Award for Best Score"]
_PROPS = [
    ("P840", _PLACES),
    ("P2408", _PERIODS),
    ("P921", _SUBJECTS),
    ("P136", _GENRES),
    ("P166", _AWARDS),
]
_PREFIX = "http://www.wikidata.org/prop/direct/"


def _wikidata_rows(imdb: str) -> list[dict[str, Any]]:
    seed = _seed("wikidata", imdb)
    if seed % 4 == 0:
        return []
    out: list[dict[str, Any]] = []
    for index, (prop, pool) in enumerate(_PROPS):
        for n in range((seed >> (index * 3)) % 3):
            label = pool[_seed(imdb, prop, n) % len(pool)]
            out.append(
                {
                    "imdb": {"type": "literal", "value": imdb},
                    "prop": {"type": "uri", "value": _PREFIX + prop},
                    "label": {"type": "literal", "value": label, "xml:lang": "en"},
                }
            )
    if seed % 6 == 0:
        out.append(
            {
                "imdb": {"value": imdb},
                "prop": {"value": _PREFIX + "P136"},
                "label": {"value": "   "},
            }
        )
    if seed % 8 == 0:
        out.append({"imdb": {"value": imdb}, "prop": {"value": _PREFIX + "P136"}})
    if seed % 10 == 0:
        out.append(
            {
                "imdb": {"value": imdb},
                "prop": {"value": "http://example.org/other"},
                "label": {"value": "ignored"},
            }
        )
    return out


# --- MDBList ratings -----------------------------------------------------------------------

_SITES = ["imdb", "tmdb", "trakt", "letterboxd", "tomatoes", "popcorn", "metacritic", "myanimelist"]


def _ratings(imdb: str) -> dict[str, Any] | None:
    seed = _seed("ratings", imdb)
    if seed % 9 == 0:
        return None
    ratings: list[dict[str, Any]] = []
    for index, site in enumerate(_SITES):
        pick = (seed >> index) & 7
        value: Any = [round((seed % 100) / 10, 1), seed % 100, 4.0, None, 8.5][pick % 5]
        votes: Any = [seed % 50000, None, 1, "12", 0][(pick + index) % 5]
        ratings.append({"source": site, "value": value, "score": 50, "votes": votes})
    if seed % 5 == 0:
        ratings.append({"source": "imdb", "value": 1.5, "votes": 3})  # a repeated site
    if seed % 11 == 0:
        ratings.append({"source": "", "value": 3})
    ids: dict[str, Any] = {"imdb": imdb}
    return {"title": "x", "ids": ids, "ratings": ratings}


# --- Letterboxd film pages -----------------------------------------------------------------

_THEMES = [
    "Heists and robbery",
    "Time travel",
    "Epic Battles",
    "Coming of age",
    "Murder mystery",
    "Dreams and nightmares",
]


def _film_page(tmdb: str, broken: bool) -> str:
    seed = _seed("film", tmdb)
    if broken and seed % 3 == 0:
        return '<html><body class="film"><h3>Themes</h3></body></html>'
    themes = [_THEMES[_seed(tmdb, n) % len(_THEMES)] for n in range(seed % 5)]
    if seed % 13 == 0:  # a layout whose Themes heading was renamed
        heading = "Topics"
    else:
        heading = "Themes"
    links = "".join(
        f'<a href="/films/{"theme" if n % 2 else "mini-theme"}/t{n}/by/best-match/">{label}</a>\n'
        for n, label in enumerate(themes)
    )
    marker = tmdb if seed % 17 else "1"
    return (
        f'<html><body class="film" data-type="film" data-tmdb-id="{marker}">'
        '<h3>Genres</h3><div class="text-sluglist"><a href="/films/genre/drama/">Drama</a></div>'
        f'<h3>{heading}</h3><div class="text-sluglist"><p>{links}</p></div></body></html>'
    )


def build(store: Path) -> Scenario:
    scenario = Scenario(mapping=_mapping(store))
    with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as conn:
        picked = conn.execute(
            "SELECT e.value FROM external_ids e JOIN items i ON i.item_id = e.item_id "
            "WHERE e.ns = 'imdb' AND i.type IN ('movie', 'show') ORDER BY i.item_id LIMIT 1 "
            "OFFSET 1234"
        ).fetchone()
    scenario.failing_imdb = picked[0] if picked else ""
    quota = os.environ.get("PARITY_FAKE_QUOTA_AFTER", "").strip()
    scenario.quota_after = int(quota) if quota else None
    scenario.lb_broken = os.environ.get("PARITY_FAKE_LB_BROKEN", "").strip() == "1"
    return scenario


def serve(scenario: Scenario) -> tuple[ThreadingHTTPServer, str]:
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def _send(
            self, status: int, body: str, kind: str = "application/json", **headers: str
        ) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            for name, value in headers.items():
                self.send_header(name.replace("_", "-"), value)
            self.end_headers()
            self.wfile.write(data)

        def _log(self, line: str) -> None:
            with lock:
                scenario.requests.append(line)

        def _body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            query = parse_qs(url.query)
            raw = self._body()
            if url.path == "/graphql":
                ids = json.loads(raw)["variables"]["ids"]
                self._log(f"POST /graphql ids={ids}")
                if FAIL_ANILIST_ID in ids:
                    self._send(500, "{}")
                    return
                media = [m for m in map(_anilist_media, ids) if m is not None]
                self._send(200, json.dumps({"data": {"Page": {"media": media}}}))
            elif url.path == "/sparql":
                text = parse_qs(raw.decode())["query"][0]
                ids = re.findall(r'"(tt\d+)"', text)
                self._log(f"POST /sparql ids={ids}")
                if scenario.failing_imdb in ids:
                    self._send(500, "{}")
                    return
                bindings = [row for imdb in ids for row in _wikidata_rows(imdb)]
                self._send(200, json.dumps({"results": {"bindings": bindings}}))
            elif re.fullmatch(r"/imdb/(movie|show)/", url.path):
                ids = json.loads(raw)["ids"]
                self._log(f"POST {url.path} ids={ids}")
                if query.get("apikey") != [API_KEY]:
                    self._send(401, "{}")
                    return
                with lock:
                    scenario.ratings_calls += 1
                    spent = (
                        scenario.quota_after is not None
                        and scenario.ratings_calls > scenario.quota_after
                    )
                if spent:
                    self._send(429, "{}", x_ratelimit_remaining="0", x_ratelimit_reset="1")
                elif scenario.failing_imdb in ids:
                    self._send(500, "{}")
                else:
                    found = [r for r in map(_ratings, ids) if r is not None]
                    self._send(200, json.dumps(found))
            else:
                self._send(404, "{}")

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/mapping.json":
                self._log("GET /mapping.json")
                self._send(200, json.dumps(scenario.mapping))
                return
            self._log(f"GET {path}")
            if path == "/robots.txt":
                self._send(200, "User-agent: *\nDisallow: /ajax/\n", "text/plain")
            elif m := re.fullmatch(r"/tmdb/(\d+)/", path):
                tmdb = m.group(1)
                seed = _seed("lookup", tmdb)
                host = self.headers.get("Host", "")
                if seed % 11 == 0:
                    self._send(200, "<title>TMDB Import Result</title>", "text/html")
                elif seed % 13 == 0:
                    self._send(404, "not found", "text/plain")
                elif seed % 101 == 3:
                    self._send(200, "<html>just a moment</html>", "text/html")
                elif seed % 7 == 0:
                    self._send(302, "", "text/plain", Location=f"http://{host}/film/m-{tmdb}/")
                elif seed % 29 == 1:
                    self._send(302, "", "text/plain", Location="https://elsewhere.test/film/x/")
                else:
                    self._send(302, "", "text/plain", Location=f"/film/m-{tmdb}/?ref=tmdb")
            elif m := re.fullmatch(r"/film/m-(\d+)/", path):
                self._send(200, _film_page(m.group(1), scenario.lb_broken), "text/html")
            else:
                self._send(404, "not found", "text/plain")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@contextlib.contextmanager
def serve_sources(store: Path) -> Iterator[tuple[dict[str, str], Scenario]]:
    """Serve the scenario built from `store`; yields the environment that points both
    implementations at it, and the scenario (its request log)."""
    scenario = build(store)
    server, url = serve(scenario)
    try:
        yield (
            {
                "ANILIST_BASE_URL": f"{url}/graphql",
                "ANILIST_MAPPING_URL": f"{url}/mapping.json",
                "ANILIST_PAUSE_SECONDS": "0",
                "WIKIDATA_BASE_URL": f"{url}/sparql",
                "WIKIDATA_PAUSE_SECONDS": "0",
                "MDBLIST_BASE_URL": url,
                "MDBLIST_API_KEY": API_KEY,
                "LETTERBOXD_BASE_URL": url,
                "LETTERBOXD_PAUSE_SECONDS": "0",
                "NO_PROXY": "127.0.0.1",
                "no_proxy": "127.0.0.1",
            },
            scenario,
        )
    finally:
        server.shutdown()
        server.server_close()
