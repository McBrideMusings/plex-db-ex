"""The tag explorer — a read-only browser view over `tmdb_keywords`.

It exists to find keywords that describe a title's packaging rather than its
subject. `duringcreditsstinger` is the case that started it: it tags 383 movies,
rides along with superhero films, and so lifts a superhero pick in a
keyword-cosine ranking for a reason that has nothing to do with taste.

Nothing here writes. Every request opens the store through `open_readonly`, so
a handler bug meets SQLite's read-only mode rather than the one writer's file
(ADR-0001). The noise list the page keeps lives in the viewer's browser, never
on disk.

The numbers are computed the way `taste-cosine.rhai` computes them for a pool,
with the whole library of one type standing in for the pool: `df` is how many
titles of that type carry the keyword, `N` is how many titles of that type carry
any keyword at all, and IDF is `1 + ln(N / df)`.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .errors import StoreError
from .store import open_readonly

#: Set in the container to have `plexdb schedule` serve the explorer beside the
#: sweep, reading the published snapshot. Unset, the scheduler serves nothing.
EXPLORE_PORT_VAR = "PLEXDB_EXPLORE_PORT"

#: The media types TMDB has keywords for. An episode never carries them
#: (`enrich_tmdb_keywords` enriches movies and shows only).
KINDS = ("movie", "show")

#: How many co-occurring tags each row names. Five is enough to show what a tag
#: travels with; the drill-down carries the rest.
CO_TAGS = 5

_NAMESPACE = "tmdb_keywords"
_KEY = "keyword"


@dataclass(frozen=True)
class Tag:
    value: str
    #: Titles of this type carrying the tag.
    df: int
    idf: float
    #: The tags it most often shares a title with, most frequent first, each
    #: with the number of titles the two share.
    co_tags: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class TagIndex:
    kind: str
    #: N: titles of this type carrying at least one keyword.
    titles: int
    #: Every keyword, most titles first, ties by name.
    tags: tuple[Tag, ...]


@dataclass(frozen=True)
class Title:
    item_id: str
    title: str
    year: int | None
    #: How many keywords the title carries in all, so a title tagged with one
    #: keyword reads differently from one tagged with forty.
    keywords: int


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")


def build_index(conn: sqlite3.Connection, kind: str, co_tags: int = CO_TAGS) -> TagIndex:
    """Every keyword on titles of `kind`, with its count, IDF and co-tags.

    Co-occurrence is counted in Python rather than as a SQL self-join: the
    movie side is about three million tag pairs, which a `Counter` walks in
    half a second and a `GROUP BY` over a self-join does not.
    """
    _check_kind(kind)
    rows = conn.execute(
        "SELECT e.item_id, e.value FROM enrichment e JOIN items i USING (item_id) "
        "WHERE e.namespace = ? AND e.key = ? AND i.type = ?",
        (_NAMESPACE, _KEY, kind),
    ).fetchall()

    by_title: defaultdict[str, list[str]] = defaultdict(list)
    for item_id, value in rows:
        by_title[item_id].append(value)
    df = Counter(value for _, value in rows)
    n = len(by_title)

    together: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for values in by_title.values():
        for a in values:
            counts = together[a]
            for b in values:
                if a != b:
                    counts[b] += 1

    def co(value: str) -> tuple[tuple[str, int], ...]:
        # most_common keeps insertion order among ties, which depends on row
        # order; sorting by name as well makes the answer the same every time.
        ranked = sorted(together[value].items(), key=lambda kv: (-kv[1], kv[0]))
        return tuple(ranked[:co_tags])

    tags = tuple(
        Tag(value=value, df=count, idf=1.0 + math.log(n / count), co_tags=co(value))
        for value, count in sorted(df.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    return TagIndex(kind=kind, titles=n, tags=tags)


def titles_tagged(conn: sqlite3.Connection, kind: str, value: str) -> list[Title]:
    """Every title of `kind` carrying the keyword `value`, by title."""
    _check_kind(kind)
    rows = conn.execute(
        "SELECT i.item_id, i.title, i.year, "
        "  (SELECT COUNT(*) FROM enrichment k "
        "   WHERE k.item_id = i.item_id AND k.namespace = e.namespace AND k.key = e.key) "
        "FROM enrichment e JOIN items i USING (item_id) "
        "WHERE e.namespace = ? AND e.key = ? AND e.value = ? AND i.type = ? "
        "ORDER BY COALESCE(i.title_sort, i.title) COLLATE NOCASE, i.year",
        (_NAMESPACE, _KEY, value, kind),
    ).fetchall()
    return [Title(item_id=r[0], title=r[1], year=r[2], keywords=r[3]) for r in rows]


def index_json(index: TagIndex) -> dict[str, object]:
    return {
        "kind": index.kind,
        "titles": index.titles,
        "tags": [
            {
                "value": tag.value,
                "df": tag.df,
                "idf": round(tag.idf, 4),
                "co": [[value, count] for value, count in tag.co_tags],
            }
            for tag in index.tags
        ],
    }


def titles_json(kind: str, value: str, titles: list[Title]) -> dict[str, object]:
    return {
        "kind": kind,
        "tag": value,
        "titles": [
            {"item_id": t.item_id, "title": t.title, "year": t.year, "keywords": t.keywords}
            for t in titles
        ],
    }


class _IndexCache:
    """One `TagIndex` per kind, rebuilt when the store file changes.

    Keyed on the file's size and modification time, so a newly published
    snapshot — which `publish` renames into place — is picked up on the next
    request without a restart.
    """

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[tuple[int, int], dict[str, object]]] = {}

    def get(self, kind: str) -> dict[str, object]:
        stat = self._path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            hit = self._entries.get(kind)
            if hit is not None and hit[0] == stamp:
                return hit[1]
            with open_readonly(self._path) as conn:
                payload = index_json(build_index(conn, kind))
            self._entries[kind] = (stamp, payload)
            return payload


def _page() -> bytes:
    return resources.files("plexdb").joinpath("explore.html").read_bytes()


def make_server(store_path: Path, host: str, port: int) -> ThreadingHTTPServer:
    """An HTTP server for the explorer, bound but not yet serving.

    The caller owns the loop and the shutdown: `plexdb explore` runs
    `serve_forever` in the foreground, and a test runs it on a thread.
    """
    cache = _IndexCache(store_path)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — the stdlib's name
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/":
                    self._send(HTTPStatus.OK, "text/html; charset=utf-8", _page())
                elif url.path == "/api/tags":
                    kind = query.get("kind", "movie")
                    _check_kind(kind)
                    self._json(cache.get(kind))
                elif url.path == "/api/titles":
                    kind = query.get("kind", "movie")
                    tag = query.get("tag")
                    if not tag:
                        raise ValueError("tag is required")
                    with open_readonly(store_path) as conn:
                        self._json(titles_json(kind, tag, titles_tagged(conn, kind, tag)))
                else:
                    self._json({"error": f"no route {url.path}"}, HTTPStatus.NOT_FOUND)
            except ValueError as err:
                self._json({"error": str(err)}, HTTPStatus.BAD_REQUEST)
            except FileNotFoundError:
                # On the host this is the normal state until the first sweep
                # publishes a snapshot, not a crash worth a traceback.
                missing = f"no store at {store_path} yet — the next sweep publishes one"
                self._json({"error": missing}, HTTPStatus.SERVICE_UNAVAILABLE)
            except StoreError as err:
                self._json({"error": str(err)}, HTTPStatus.SERVICE_UNAVAILABLE)

        def _json(self, body: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
            data = json.dumps(body, separators=(",", ":")).encode()
            self._send(status, "application/json", data)

        def _send(self, status: HTTPStatus, content_type: str, data: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            # One line per request on stderr is noise for a single-user tool.
            pass

    return ThreadingHTTPServer((host, port), Handler)


def serve_in_background(store_path: Path, host: str, port: int) -> ThreadingHTTPServer:
    """Start the explorer on a daemon thread and return its server.

    This is how the container serves it: a thread inside `plexdb schedule`, so
    one image and one `[docker_run]` carry both. A daemon thread dies with the
    scheduler rather than holding the container up after it, and an exception
    in one request stays in that request's own thread.
    """
    server = make_server(store_path, host, port)
    threading.Thread(target=server.serve_forever, name="explore", daemon=True).start()
    return server
