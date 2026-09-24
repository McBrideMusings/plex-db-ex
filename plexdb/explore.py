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
from collections.abc import Callable
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


#: Tags drawn around the centre of a neighbourhood graph when the caller does
#: not say. Forty is readable on one screen; the page offers more.
NEIGHBOURS = 40

#: Each node keeps only its strongest links. Forty tags that all co-occur
#: somewhere would otherwise draw 780 edges and a solid disc.
EDGES_PER_NODE = 4


@dataclass(frozen=True)
class Neighbourhood:
    kind: str
    centre: str
    #: The centre first, then its co-occurring tags, most shared titles first.
    #: Each is `(tag, titles carrying it, titles it shares with the centre)`.
    nodes: tuple[tuple[str, int, int], ...]
    #: `(a, b, shared titles)`, each pair once.
    edges: tuple[tuple[str, str, int], ...]


def neighbourhood(
    conn: sqlite3.Connection,
    kind: str,
    centre: str,
    size: int = NEIGHBOURS,
    exclude: frozenset[str] = frozenset(),
    edges_per_node: int = EDGES_PER_NODE,
) -> Neighbourhood:
    """The `size` tags that share the most titles with `centre`, and the
    strongest links among them.

    `exclude` drops tags before they are ranked, so a tag marked as noise frees
    its place for the next one rather than leaving a hole.
    """
    _check_kind(kind)
    shared = conn.execute(
        "SELECT o.value, COUNT(*) FROM enrichment c "
        "JOIN enrichment o ON o.item_id = c.item_id AND o.namespace = c.namespace "
        "  AND o.key = c.key AND o.value != c.value "
        "JOIN items i ON i.item_id = c.item_id "
        "WHERE c.namespace = ? AND c.key = ? AND c.value = ? AND i.type = ? "
        "GROUP BY o.value ORDER BY COUNT(*) DESC, o.value",
        (_NAMESPACE, _KEY, centre, kind),
    ).fetchall()
    ranked = [(value, count) for value, count in shared if value not in exclude][:size]
    members = [centre, *(value for value, _ in ranked)]

    marks = ",".join("?" * len(members))
    rows = conn.execute(
        f"SELECT e.item_id, e.value FROM enrichment e JOIN items i USING (item_id) "
        f"WHERE e.namespace = ? AND e.key = ? AND i.type = ? AND e.value IN ({marks})",
        (_NAMESPACE, _KEY, kind, *members),
    ).fetchall()
    by_title: defaultdict[str, list[str]] = defaultdict(list)
    for item_id, value in rows:
        by_title[item_id].append(value)
    df = Counter(value for _, value in rows)
    pairs: Counter[tuple[str, str]] = Counter()
    for values in by_title.values():
        values.sort()
        for i, a in enumerate(values):
            for b in values[i + 1 :]:
                pairs[(a, b)] += 1

    strongest: defaultdict[str, list[tuple[int, str, str]]] = defaultdict(list)
    for (a, b), count in pairs.items():
        strongest[a].append((count, a, b))
        strongest[b].append((count, a, b))
    kept: set[tuple[str, str, int]] = set()
    for links in strongest.values():
        links.sort(key=lambda link: (-link[0], link[1], link[2]))
        kept.update((a, b, count) for count, a, b in links[:edges_per_node])

    with_centre = dict(ranked)
    nodes = tuple((value, df[value], with_centre.get(value, df[value])) for value in members)
    edges = tuple(sorted(kept, key=lambda e: (-e[2], e[0], e[1])))
    return Neighbourhood(kind=kind, centre=centre, nodes=nodes, edges=edges)


def neighbourhood_json(hood: Neighbourhood) -> dict[str, object]:
    return {
        "kind": hood.kind,
        "centre": hood.centre,
        "nodes": [{"value": v, "df": df, "shared": s} for v, df, s in hood.nodes],
        "edges": [[a, b, n] for a, b, n in hood.edges],
    }


#: A tag on fewer titles than this cannot place a title near any other, so it
#: is left out of the map's vectors.
MAP_MIN_DF = 2

#: Dimensions the keyword vectors are reduced to before UMAP, which is slow
#: and noisy on 19,000 sparse columns and fine on 50 dense ones.
MAP_SVD_COMPONENTS = 50

#: UMAP's neighbourhood size: how many nearest titles each title's position is
#: pulled towards. Its own default; lowered only when a store is too small for it.
#: Larger values pack the centre of the movie map tighter.
MAP_NEIGHBOURS = 15

#: UMAP's `min_dist` and `spread`: how tightly it may pack points together. A
#: larger `min_dist` spreads a dense cluster out instead of piling it up.
MAP_MIN_DIST = 0.8
MAP_SPREAD = 1.5

#: Each axis is clipped to these percentiles before scaling to the unit square,
#: so a few far outliers do not shrink everyone else. Points beyond land on the border.
MAP_CLIP = (1.0, 99.0)

#: Names how a map is drawn. It is part of a stored map's fingerprint, so
#: changing the algorithm or any constant above and bumping this string makes
#: every stored map stale and the next refresh redraws it.
MAP_RECIPE = (
    f"umap-cosine-svd{MAP_SVD_COMPONENTS}-df{MAP_MIN_DF}-nn{MAP_NEIGHBOURS}"
    f"-md{MAP_MIN_DIST}-sp{MAP_SPREAD}-clip{MAP_CLIP[0]:g}-{MAP_CLIP[1]:g}"
)


@dataclass(frozen=True)
class MapPoint:
    item_id: str
    title: str
    year: int | None
    #: Position in the unit square. Only distances mean anything.
    x: float
    y: float


@dataclass(frozen=True)
class TitleMap:
    kind: str
    points: tuple[MapPoint, ...]
    #: Titles with keywords that are not on the map: none of their tags is
    #: shared with another title once noise is excluded.
    unplaced: int


def title_map(
    conn: sqlite3.Connection, kind: str, exclude: frozenset[str] = frozenset(), seed: int = 0
) -> TitleMap:
    """Every title of `kind` placed in 2D so titles with similar keywords sit close.

    A title's vector is its tags weighted by the same IDF the table shows,
    normalised to unit length so a title with forty tags is not louder than one
    with five — the cosine a keyword scorer compares. Truncated SVD takes it to
    `MAP_SVD_COMPONENTS` dimensions and UMAP (cosine distance) to two. `seed`
    fixes both, so the same store and the same exclusions draw the same map.

    numpy and scikit-learn are imported here rather than at the top of the
    module, so every other `plexdb` command starts without loading them.
    """
    import numpy as np
    import umap
    from scipy.sparse import csr_matrix
    from sklearn.decomposition import TruncatedSVD
    from sklearn.preprocessing import normalize

    _check_kind(kind)
    rows = conn.execute(
        "SELECT e.item_id, e.value, i.title, i.year FROM enrichment e JOIN items i USING (item_id) "
        "WHERE e.namespace = ? AND e.key = ? AND i.type = ?",
        (_NAMESPACE, _KEY, kind),
    ).fetchall()
    carrying: set[str] = {r[0] for r in rows}
    df = Counter(r[1] for r in rows)
    n = len(carrying)
    vocab = {
        value: col
        for col, value in enumerate(
            sorted(v for v, c in df.items() if c >= MAP_MIN_DF and v not in exclude)
        )
    }

    placed: dict[str, int] = {}
    about: dict[str, tuple[str, int | None]] = {}
    cells_r: list[int] = []
    cells_c: list[int] = []
    cells_v: list[float] = []
    for item_id, value, title, year in rows:
        col = vocab.get(value)
        if col is None:
            continue
        row = placed.setdefault(item_id, len(placed))
        about[item_id] = (title, year)
        cells_r.append(row)
        cells_c.append(col)
        cells_v.append(1.0 + math.log(n / df[value]))

    ids = sorted(placed, key=placed.__getitem__)
    if len(ids) < 5 or len(vocab) < 3:
        # Too few titles for a layout to mean anything; lay them on a line.
        coords = np.array([[i / max(1, len(ids) - 1), 0.5] for i in range(len(ids))])
    else:
        matrix = csr_matrix((cells_v, (cells_r, cells_c)), shape=(len(ids), len(vocab)))
        matrix = normalize(matrix)
        components = min(MAP_SVD_COMPONENTS, len(vocab) - 1, len(ids) - 1)
        reduced = TruncatedSVD(n_components=components, random_state=seed).fit_transform(matrix)
        reduced = normalize(reduced)
        coords = umap.UMAP(
            n_components=2,
            n_neighbors=min(MAP_NEIGHBOURS, len(ids) - 1),
            metric="cosine",
            min_dist=MAP_MIN_DIST,
            spread=MAP_SPREAD,
            random_state=seed,
            n_jobs=1,  # a seed already forces one thread; saying so silences UMAP's warning
        ).fit_transform(reduced)
        low, high = np.percentile(coords, MAP_CLIP, axis=0)
        coords = np.clip(coords, low, high)
        coords = (coords - low) / np.where(high > low, high - low, 1.0)

    points = tuple(
        MapPoint(
            item_id=item_id,
            title=about[item_id][0],
            year=about[item_id][1],
            x=round(float(coords[i][0]), 5),
            y=round(float(coords[i][1]), 5),
        )
        for i, item_id in enumerate(ids)
    )
    return TitleMap(kind=kind, points=points, unplaced=n - len(ids))


def keyword_fingerprint(conn: sqlite3.Connection, kind: str) -> str:
    """A cheap digest of what the default map of `kind` is drawn from.

    The recipe, the number of keyword rows, the number of titles carrying them
    and the newest `fetched_at` among them. A re-fetch stamps a new
    `fetched_at`, a wipe or a title leaving `items` changes the counts, and a
    change to the drawing code changes `MAP_RECIPE`. Nothing reads it except
    the comparison in `stored_map` and `plexdb.titlemap`.
    """
    _check_kind(kind)
    rows, titles, newest = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT e.item_id), MAX(e.fetched_at) "
        "FROM enrichment e JOIN items i USING (item_id) "
        "WHERE e.namespace = ? AND e.key = ? AND i.type = ?",
        (_NAMESPACE, _KEY, kind),
    ).fetchone()
    return f"{MAP_RECIPE}|{rows}|{titles}|{newest or ''}"


def stored_map(conn: sqlite3.Connection, kind: str) -> dict[str, object] | None:
    """The default map the writer stored, as `title_map_json` would give it, or
    `None` when there is none or it was drawn from different keyword rows.

    `None` is the caller's cue to draw it live. A store that predates the
    `title_map` tables has neither, and reads as having no stored map.
    """
    _check_kind(kind)
    try:
        state = conn.execute(
            "SELECT fingerprint, unplaced FROM title_map_state WHERE kind = ?", (kind,)
        ).fetchone()
        if state is None or state[0] != keyword_fingerprint(conn, kind):
            return None
        rows = conn.execute(
            "SELECT m.item_id, i.title, i.year, m.x, m.y FROM title_map m "
            "JOIN items i USING (item_id) WHERE m.kind = ? ORDER BY m.item_id",
            (kind,),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    return {
        "kind": kind,
        "unplaced": state[1],
        "points": [[r[0], r[1], r[2], r[3], r[4]] for r in rows],
    }


def title_map_json(tmap: TitleMap) -> dict[str, object]:
    return {
        "kind": tmap.kind,
        "unplaced": tmap.unplaced,
        # Arrays rather than objects: ten thousand points, and the keys would
        # be most of the payload.
        "points": [[p.item_id, p.title, p.year, p.x, p.y] for p in tmap.points],
    }


def title_keywords(conn: sqlite3.Connection, item_id: str) -> list[str]:
    """One title's keywords, by name."""
    rows = conn.execute(
        "SELECT value FROM enrichment WHERE item_id = ? AND namespace = ? AND key = ? "
        "ORDER BY value",
        (item_id, _NAMESPACE, _KEY),
    ).fetchall()
    return [r[0] for r in rows]


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


class _KeyedCache:
    """Computed values by key: one compute per key, and no key waits on another.

    A short-held guard protects the entries and the computes in flight. The
    first request for a key computes it; a second request for the same key
    waits on that compute's event and then looks again, so it reuses the result,
    or, if the compute failed, becomes the next one to compute. A request for
    any other key never touches the event. Each entry carries the generation it
    was computed for, so a newer generation replaces it rather than sitting
    beside it, and the oldest entry goes once more than `keep` are held.
    """

    def __init__(self, keep: int) -> None:
        self._keep = keep
        self._guard = threading.Lock()
        self._flights: dict[tuple[object, object], threading.Event] = {}
        self._entries: dict[object, tuple[object, dict[str, object]]] = {}

    def get(
        self, key: object, generation: object, compute: Callable[[], dict[str, object]]
    ) -> dict[str, object]:
        while True:
            with self._guard:
                hit = self._entries.get(key)
                if hit is not None and hit[0] == generation:
                    return hit[1]
                flight = self._flights.get((key, generation))
                if flight is None:
                    flight = self._flights[(key, generation)] = threading.Event()
                    mine = True
                else:
                    mine = False
            if not mine:
                flight.wait()
                continue
            try:
                value = compute()
                with self._guard:
                    self._entries.pop(key, None)
                    self._entries[key] = (generation, value)
                    while len(self._entries) > self._keep:
                        del self._entries[next(iter(self._entries))]
                return value
            finally:
                with self._guard:
                    del self._flights[(key, generation)]
                flight.set()


class _IndexCache:
    """One `TagIndex` per kind, rebuilt when the store file changes.

    Generation is the file's size and modification time, so a newly published
    snapshot — which `publish` renames into place — is picked up on the next
    request without a restart.
    """

    KEEP = 2

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._cache = _KeyedCache(self.KEEP)

    def get(self, kind: str) -> dict[str, object]:
        stat = self._path.stat()

        def compute() -> dict[str, object]:
            with open_readonly(self._path) as conn:
                return index_json(build_index(conn, kind))

        return self._cache.get(kind, (stat.st_mtime_ns, stat.st_size), compute)


class _MapCache:
    """Computed maps, keyed on kind and exclusions, per store file stamp.

    A map takes tens of seconds, so a repeat of the same request must not pay
    again, and a per-key lock keeps two browser tabs from computing the same
    map at once without making a Shows request wait behind a Movies one. A
    handful of entries covers flipping between Movies and Shows while marking
    noise; the oldest goes first.
    """

    KEEP = 6

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._cache = _KeyedCache(self.KEEP)

    def get(self, kind: str, exclude: frozenset[str]) -> dict[str, object]:
        stat = self._path.stat()

        def compute() -> dict[str, object]:
            with open_readonly(self._path) as conn:
                payload = None if exclude else stored_map(conn, kind)
                if payload is None:
                    payload = title_map_json(title_map(conn, kind, exclude))
            return payload

        return self._cache.get((kind, exclude), (stat.st_mtime_ns, stat.st_size), compute)


def _page() -> bytes:
    return resources.files("plexdb").joinpath("explore.html").read_bytes()


def make_server(store_path: Path, host: str, port: int) -> ThreadingHTTPServer:
    """An HTTP server for the explorer, bound but not yet serving.

    The caller owns the loop and the shutdown: `plexdb explore` runs
    `serve_forever` in the foreground, and a test runs it on a thread.
    """
    cache = _IndexCache(store_path)
    maps = _MapCache(store_path)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — the stdlib's name
            url = urlparse(self.path)
            lists = parse_qs(url.query)
            query = {k: v[0] for k, v in lists.items()}
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
                elif url.path == "/api/graph":
                    kind = query.get("kind", "movie")
                    tag = query.get("tag")
                    if not tag:
                        raise ValueError("tag is required")
                    size = query.get("size", str(NEIGHBOURS))
                    if not size.isdigit() or not 1 <= int(size) <= 200:
                        raise ValueError("size must be a number from 1 to 200")
                    # Repeated `exclude=` rather than one comma list: a TMDB
                    # keyword can itself contain a comma.
                    exclude = frozenset(lists.get("exclude", []))
                    with open_readonly(store_path) as conn:
                        hood = neighbourhood(conn, kind, tag, int(size), exclude)
                    self._json(neighbourhood_json(hood))
                elif url.path == "/api/map":
                    kind = query.get("kind", "movie")
                    _check_kind(kind)
                    self._json(maps.get(kind, frozenset(lists.get("exclude", []))))
                elif url.path == "/api/title":
                    item_id = query.get("item_id")
                    if not item_id:
                        raise ValueError("item_id is required")
                    with open_readonly(store_path) as conn:
                        keywords = title_keywords(conn, item_id)
                    self._json({"item_id": item_id, "keywords": keywords})
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
