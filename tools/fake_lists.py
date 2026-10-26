"""A recorded MDBList and Letterboxd for `tools/parity.py`.

The crowd-list harvests read two live services whose answers change between two runs and
whose daily quota is spent by a real run. To compare the Python and Rust harvests, both are
pointed at this one local server, which answers the same bytes to each. The answers are
built from the store being compared, so the lists name titles the library really holds:
what the harvests resolve, skip, place twice and fail on is decided by the store's own rows.

The server speaks the two services' shapes as `crates/plexdb/src/mdblist.rs` and
`crates/plexdb/src/letterboxd.rs` document them, and logs every request (path and query, minus
the key) so a run's requests can be compared as well as its rows.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

API_KEY = "parity-key"
USER = "parity"

#: The seed query of `plexdb/list_search.py` (`_SEEDS`).
_SEEDS = """
SELECT w.item_id, i.title, COUNT(*) AS plays
FROM (
    SELECT p.item_id AS item_id FROM plays p JOIN items m ON m.item_id = p.item_id
     WHERE m.type = 'movie'
    UNION ALL
    SELECT e.show_item_id FROM plays p JOIN items e ON e.item_id = p.item_id
     WHERE e.type = 'episode' AND e.show_item_id IS NOT NULL
) w JOIN items i ON i.item_id = w.item_id
GROUP BY w.item_id
ORDER BY plays DESC, w.item_id
"""


@dataclass
class Scenario:
    """Everything the server answers, and the rows both runs start with."""

    #: `/lists/top`.
    top: list[dict[str, Any]] = field(default_factory=list)
    #: Every list by id: its meta, and how its items answer.
    metas: dict[int, dict[str, Any]] = field(default_factory=dict)
    items: dict[int, Any] = field(default_factory=dict)
    #: Search query (lowercased) -> list metas; `failing_queries` answer 500.
    search: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    failing_queries: set[str] = field(default_factory=set)
    #: Letterboxd pages by path, and films by slug.
    pages: dict[str, str] = field(default_factory=dict)
    films: dict[str, tuple[int, str]] = field(default_factory=dict)
    #: `list_decision` rows both stores start with, and the file Plex DB-EX wrote.
    decisions: list[tuple[str, str, str, int]] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)

    def seed(self, conn: sqlite3.Connection) -> None:
        """Write the decision rows the harvests act on."""
        with conn:
            conn.executemany(
                "INSERT INTO list_decision (url, decision, decided_at, added) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (url) DO UPDATE SET decision = excluded.decision, "
                "decided_at = excluded.decided_at, added = excluded.added",
                self.decisions,
            )


def _meta(list_id: int, name: str, slug: str, size: int, likes: int) -> dict[str, Any]:
    return {
        "id": list_id,
        "name": name,
        "slug": slug,
        "user_name": USER,
        "items": size,
        "likes": likes,
        "mediatype": "movie",
        "dynamic": False,
    }


def _imdb_entry(imdb: str, title: str, year: int) -> dict[str, Any]:
    return {
        "id": 0,
        "mediatype": "movie",
        "ids": {"imdb": imdb},
        "title": title,
        "release_year": year,
        "rank": 99,
    }


def _tmdb_entry(tmdb: str, title: str) -> dict[str, Any]:
    return {"id": int(tmdb), "mediatype": "movie", "ids": {"tmdb": int(tmdb)}, "title": title}


def _page(body: str, **extra: str) -> str:
    return f"<!DOCTYPE html><html><head>{extra.get('head', '')}</head>{body}</html>"


def _list_page(name: str, size: int, films: list[tuple[str, str]], nxt: str | None) -> str:
    items = "\n".join(
        f'<li class="poster-container"><div data-item-slug="{slug}" '
        f'data-item-name="{label}"></div></li>'
        for slug, label in films
    )
    link = f'<a class="next" href="{nxt}">Older</a>' if nxt else ""
    return _page(
        f'<body class="list-page"><h1 class="title-1 prettify">{name}</h1>'
        f"<ul>{items}</ul>{link}"
        "<script>var x = \"<div data-item-slug='decoy' data-item-name='Decoy (1999)'>\";</script>"
        "</body>",
        head=f'<meta name="description" content="A list of {size} films compiled on Letterboxd">',
    )


def build(store: Path) -> Scenario:
    """The scenario for one store, read-only. Deterministic for a given store."""
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    movies = [
        (r["item_id"], r["value"], r["title"], r["year"])
        for r in conn.execute(
            "SELECT i.item_id, x.value, i.title, i.year FROM items i "
            "JOIN external_ids x ON x.item_id = i.item_id AND x.ns = 'imdb' AND x.kind = 'movie' "
            "WHERE i.type = 'movie' ORDER BY i.item_id LIMIT 700"
        )
    ]
    tmdb_movies = {
        r["item_id"]: r["value"]
        for r in conn.execute(
            "SELECT x.item_id, x.value FROM external_ids x JOIN items i ON i.item_id = x.item_id "
            "WHERE x.ns = 'tmdb' AND x.kind = 'movie' AND i.type = 'movie' "
            "ORDER BY x.item_id LIMIT 700"
        )
    }
    shows = [
        (r["item_id"], r["value"], r["title"], r["year"])
        for r in conn.execute(
            "SELECT i.item_id, x.value, i.title, i.year FROM items i "
            "JOIN external_ids x ON x.item_id = i.item_id AND x.ns = 'imdb' AND x.kind = 'show' "
            "WHERE i.type = 'show' ORDER BY i.item_id LIMIT 60"
        )
    ]
    tmdb_shows = [
        (r["item_id"], r["value"], r["title"])
        for r in conn.execute(
            "SELECT x.item_id, x.value, i.title FROM external_ids x "
            "JOIN items i ON i.item_id = x.item_id "
            "WHERE x.ns = 'tmdb' AND x.kind = 'show' AND i.type = 'show' "
            "ORDER BY x.item_id LIMIT 20"
        )
    ]
    seeds = [(r["item_id"], r["title"]) for r in conn.execute(_SEEDS).fetchall()[:60] if r["title"]]
    stored = conn.execute(
        "SELECT collection_id, url, name FROM collection WHERE source = 'mdblist' "
        "AND url IS NOT NULL ORDER BY collection_id"
    ).fetchall()
    conn.close()

    scenario = Scenario()
    decided_at = "2026-01-01T00:00:00+00:00"

    # --- MDBList: the feed ---------------------------------------------------------------
    first = _meta(910001, "Parity Movies", "movies", 90, 500)
    entries: list[dict[str, Any]] = []
    for index, (item_id, imdb, title, year) in enumerate(movies[:60]):
        if index % 3 == 0 and item_id in tmdb_movies:
            entries.append(_tmdb_entry(tmdb_movies[item_id], title))
        elif index % 3 == 1:
            entries.append(
                {"imdb_id": imdb, "mediatype": "movie", "title": title, "release_year": year}
            )
        else:
            entries.append(_imdb_entry(imdb, title, year or 0))
    entries.append(_imdb_entry(movies[0][1], "Listed Twice", 2000))
    entries += [_imdb_entry(f"tt9{n:06d}", f"Not In Library {n}", 1990 + n) for n in range(8)]
    entries.append({"ids": {"imdb": "", "tmdb": None}, "title": "No Ids At All"})
    scenario.items[910001] = {"movies": entries, "shows": [], "pagination": {"has_more": False}}
    second = _meta(910002, "Parity Shows", "shows", len(shows), 40) | {"mediatype": "show"}
    scenario.items[910002] = {
        "movies": [],
        "shows": [
            {"ids": {"imdb": imdb, "tvdb": 1}, "title": title, "release_year": year}
            for _, imdb, title, year in shows
        ]
        + [{"ids": {"tmdb": int(tmdb)}, "title": title} for _, tmdb, title in tmdb_shows],
        "pagination": {"has_more": False},
    }
    big = _meta(910003, "Parity Big", "big", 1200, 9000)
    scenario.items[910003] = [
        [_imdb_entry(imdb, title, year or 0) for _, imdb, title, year in movies[:300]]
        + [_imdb_entry(f"tt8{n:06d}", f"Far Away {n}", 2001) for n in range(900)]
    ]
    broken = _meta(910004, "Parity Gone", "gone", 10, 1)
    scenario.items[910004] = 404
    not_an_object = _meta(910005, "Parity Odd", "odd", 10, 1)
    scenario.items[910005] = ["not", "an", "object"]
    nameless = {"id": 910006, "name": "", "items": 3, "likes": None}
    scenario.items[910006] = {
        "movies": [_imdb_entry(imdb, title, year or 0) for _, imdb, title, year in movies[60:70]],
        "shows": [],
        "pagination": {"has_more": False},
    }
    feed = [first, second, big, broken, not_an_object, nameless]
    # Two lists the baseline already stores: one fetched again or kept cached by its own age.
    for collection_id, url, name in stored[:1] + stored[-1:]:
        list_id = int(str(collection_id).split(":")[1])
        parts = [p for p in urlparse(url).path.split("/") if p]
        feed.append(
            {
                "id": list_id,
                "name": name or "stored",
                "slug": parts[-1],
                "user_name": parts[-2],
                "items": 20,
                "likes": 7,
            }
        )
        scenario.items[list_id] = {
            "movies": [
                _imdb_entry(imdb, title, year or 0) for _, imdb, title, year in movies[70:90]
            ],
            "shows": [],
            "pagination": {"has_more": False},
        }
    scenario.top = feed
    for meta in feed:
        scenario.metas[meta["id"]] = meta

    # --- MDBList: a list a person added that the feed does not carry ---------------------
    extra = _meta(910007, "Added By Hand", "extra-list", 25, 3)
    scenario.metas[910007] = extra
    scenario.items[910007] = {
        "movies": [_imdb_entry(imdb, title, year or 0) for _, imdb, title, year in movies[100:125]],
        "shows": [],
        "pagination": {"has_more": False},
    }
    scenario.decisions.append(
        (f"https://mdblist.com/lists/{USER}/extra-list", "added", decided_at, 1)
    )
    if stored:
        # A stored feed list a person excluded: wiped, and never fetched.
        scenario.decisions.append(
            (
                _canonical(stored[1][1]) if len(stored) > 1 else _canonical(stored[0][1]),
                "excluded",
                decided_at,
                0,
            )
        )
    scenario.decisions.append(
        (f"https://mdblist.com/lists/{USER}/movies", "refetch", "2099-01-01T00:00:00+00:00", 0)
    )

    # --- MDBList: the search ---------------------------------------------------------------
    pool: list[dict[str, Any]] = []
    for n in range(14):
        meta = _meta(920000 + n, f"Search Pool {n}", f"pool-{n}", 30, n)
        pool.append(meta)
        scenario.metas[meta["id"]] = meta
        # Half of them hold enough of the library to be kept; the rest hold two.
        take = 30 if n % 2 == 0 else 2
        offset = (n * 7) % max(1, len(movies) - take)
        picked = movies[offset : offset + take]
        picked += [m for m in movies if m[0] in {s[0] for s in seeds[:5]}][:3]
        scenario.items[meta["id"]] = {
            "movies": [_imdb_entry(imdb, title, year or 0) for _, imdb, title, year in picked],
            "shows": [],
            "pagination": {"has_more": False},
        }
    for index, (_, title) in enumerate(seeds):
        key = title.lower()
        if index == 2:
            scenario.failing_queries.add(key)
            continue
        scenario.search[key] = [pool[(index + step) % len(pool)] for step in range(index % 3 + 1)]

    # --- Letterboxd ----------------------------------------------------------------------
    owned = list(tmdb_movies.items())
    films: list[tuple[str, str]] = []
    for index, (_, tmdb) in enumerate(owned[:45]):
        title = f"Film {index} &amp; Co"
        slug = f"film-{index}"
        scenario.films[slug] = (int(tmdb), "movie")
        films.append((slug, f"{title} ({1950 + index})"))
    films.append(("unmatched-film", "Unmatched &#039;Film&#039; (2001)"))
    films.append(("film-0", "Film 0 &amp; Co (1950)"))
    for index, (_, tmdb, _) in enumerate(tmdb_shows[:5]):
        slug = f"series-{index}"
        scenario.films[slug] = (int(tmdb), "tv")
        films.append((slug, f"Series {index}"))
    base = f"/{USER}/list/films/"
    scenario.pages[base] = _list_page(
        "Parity &amp; Films", len(films), films[:30], f"{base}page/2/"
    )
    scenario.pages[f"{base}page/2/"] = _list_page(
        "Parity &amp; Films", len(films), films[30:], None
    )
    scenario.pages[f"/{USER}/list/short/"] = _list_page("Short", 50, films[:3], None)
    scenario.pages[f"/{USER}/list/broken/"] = "<html><body><h1>Just a moment...</h1></body></html>"
    scenario.pages[f"/{USER}/list/shows/"] = _list_page("Parity Shows", 5, films[-5:], None)
    for name in ("films", "short", "broken", "shows"):
        scenario.decisions.append(
            (f"https://letterboxd.com/{USER}/list/{name}", "added", decided_at, 1)
        )
    return scenario


def _canonical(url: str) -> str:
    parts = [unquote(p) for p in urlparse(url).path.split("/") if p]
    return f"https://mdblist.com/lists/{parts[-2].lower()}/{parts[-1].lower()}"


def _film_html(tmdb: int | None, kind: str) -> str:
    attrs = f' data-tmdb-id="{tmdb}" data-tmdb-type="{kind}"' if tmdb else ""
    return f'<html><body class="film" data-type="film"{attrs}></body></html>'


def serve(scenario: Scenario) -> tuple[ThreadingHTTPServer, str]:
    """Start the server on a free port; returns it and its base URL."""

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

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            query = parse_qs(url.query)
            shown = {k: v for k, v in query.items() if k != "apikey"}
            scenario.requests.append(
                url.path + (f"?{json.dumps(shown, sort_keys=True)}" if shown else "")
            )
            if url.path == "/robots.txt":
                self._send(200, "User-agent: *\nDisallow: /ajax/\n", "text/plain")
            elif url.path.startswith("/lists/"):
                self._mdblist(url.path, query)
            elif url.path in scenario.pages:
                self._send(200, scenario.pages[url.path], "text/html")
            else:
                match = re.fullmatch(r"/film/([^/]+)/", url.path)
                if match and match.group(1) in scenario.films:
                    tmdb, kind = scenario.films[match.group(1)]
                    self._send(200, _film_html(tmdb, kind), "text/html")
                elif match and match.group(1) == "unmatched-film":
                    self._send(200, _film_html(None, "movie"), "text/html")
                else:
                    self._send(404, "not found", "text/plain")

        def _mdblist(self, path: str, query: dict[str, list[str]]) -> None:
            if query.get("apikey") != [API_KEY]:
                self._send(401, "{}")
                return
            parts = [unquote(p) for p in path.split("/") if p]
            if parts == ["lists", "top"]:
                self._send(200, json.dumps(scenario.top))
            elif parts == ["lists", "search"]:
                text = query.get("query", [""])[0].lower()
                if text in scenario.failing_queries:
                    self._send(500, "{}")
                else:
                    self._send(200, json.dumps(scenario.search.get(text, [])))
            elif len(parts) == 3 and parts[2] == "items":
                content = scenario.items.get(int(parts[1]))
                if content == 404:
                    self._send(404, "{}")
                elif isinstance(content, list) and content and isinstance(content[0], list):
                    offset = int(query.get("offset", ["0"])[0])
                    limit = int(query.get("limit", ["1000"])[0])
                    entries = content[0]
                    page = entries[offset : offset + limit]
                    self._send(
                        200,
                        json.dumps(
                            {
                                "movies": page,
                                "shows": [],
                                "pagination": {
                                    "limit": limit,
                                    "offset": offset,
                                    "total": len(entries),
                                    "has_more": offset + limit < len(entries),
                                },
                            }
                        ),
                    )
                else:
                    self._send(200, json.dumps(content))
            elif len(parts) == 3:
                for meta in scenario.metas.values():
                    if meta.get("user_name") == parts[1] and meta.get("slug") == parts[2]:
                        self._send(200, json.dumps([meta]))
                        return
                self._send(404, "{}")
            else:
                self._send(404, "{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@contextlib.contextmanager
def serve_lists(store: Path) -> Iterator[tuple[dict[str, str], Scenario]]:
    """Serve the scenario built from `store`; yields the environment that points both
    implementations at it, and the scenario (its decision rows and request log)."""
    scenario = build(store)
    server, url = serve(scenario)
    try:
        yield (
            {
                "MDBLIST_BASE_URL": url,
                "LETTERBOXD_BASE_URL": url,
                "MDBLIST_API_KEY": API_KEY,
                "LETTERBOXD_PAUSE_SECONDS": "0",
            },
            scenario,
        )
    finally:
        server.shutdown()
