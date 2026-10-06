"""Plex TVX — a read-only browser view over the `keywords` namespace.

It exists to find keywords that describe a title's packaging rather than its
subject. `duringcreditsstinger` is the case that started it: it tags 383 movies,
rides along with superhero films, and so lifts a superhero pick in a
keyword-cosine ranking for a reason that has nothing to do with taste.

Nothing here writes the store. Every request opens it through `open_readonly`, so
a handler bug meets SQLite's read-only mode rather than the one writer's file
(ADR-0001). The files Plex TVX does write sit beside each other: the Query
tab's saved queries, `explore-queries.json` (`SavedQueries`), and the Merges and
Roles tabs' decisions files (`decisions.DecisionsFile`), each capped in count and
total size since Plex TVX has no login. The noise list the page keeps lives in the
viewer's browser, never on disk.

The numbers are computed the way `taste-cosine.rhai` computes them for a pool,
with the whole library of one type standing in for the pool: `df` is how many
titles of that type carry the keyword, `N` is how many titles of that type carry
any keyword at all, and IDF is `1 + ln(N / df)`.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import sys
import threading
import time
import traceback
from collections import Counter, OrderedDict, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, NamedTuple, cast
from urllib.parse import parse_qs, urlparse

import httpx

from .decisions import SAVED_FILE
from .errors import DecisionsFileError, StoreError
from .identity import PRIORITY
from .merge_review import (
    MERGE_DECISIONS_FILE,
    MergeDecisions,
    NoSuchPair,
    require_pair,
    review_rows,
)
from .role_review import (
    ROLE_DECISIONS_FILE,
    NoSuchRole,
    RoleDecisions,
    require_role,
    role_cell,
    roles_json,
)
from .store import open_readonly
from .tmdb_edges import SIMILAR_EDGE_TYPE

#: Set in the container to have `plexdb schedule` serve Plex TVX beside the
#: sweep, reading the published snapshot. Unset, the scheduler serves nothing.
EXPLORE_PORT_VAR = "PLEXDB_EXPLORE_PORT"

#: The media types TMDB has keywords for. An episode never carries them
#: (`enrich_tmdb_keywords` enriches movies and shows only).
KINDS = ("movie", "show")

#: How many co-occurring tags each row names. Five is enough to show what a tag
#: travels with; the drill-down carries the rest.
CO_TAGS = 5

#: Renamed from the TMDB-specific `tmdb_keywords` by ADR-0016, which moved the
#: source into its own column — Plex TVX reads across every source.
_NAMESPACE = "keywords"
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
    # DISTINCT so a keyword two sources both list on one title (ADR-0016's
    # `source` column lets both rows exist) is one entry in that title's tag
    # set, not two — both the per-tag title count (`df`) and every title's
    # own tag list below would otherwise double-count it.
    rows = conn.execute(
        "SELECT k.item_id, k.value FROM "
        "(SELECT DISTINCT item_id, value FROM enrichment WHERE namespace = ? AND key = ?) k "
        "JOIN items i USING (item_id) WHERE i.type = ?",
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
    # DISTINCT on the outer query, and COUNT(DISTINCT ...) on the inner one:
    # a title tagged `value` by two sources must appear once in this list, not
    # once per source, and its own keyword count must count each distinct
    # keyword once regardless of how many sources agree on it.
    rows = conn.execute(
        "SELECT DISTINCT i.item_id, i.title, i.year, "
        "  (SELECT COUNT(DISTINCT k.value) FROM enrichment k "
        "   WHERE k.item_id = i.item_id AND k.namespace = ? AND k.key = ?) "
        "FROM enrichment e JOIN items i USING (item_id) "
        "WHERE e.namespace = ? AND e.key = ? AND e.value = ? AND i.type = ? "
        "ORDER BY COALESCE(i.title_sort, i.title) COLLATE NOCASE, i.year",
        (_NAMESPACE, _KEY, _NAMESPACE, _KEY, value, kind),
    ).fetchall()
    return [Title(item_id=r[0], title=r[1], year=r[2], keywords=r[3]) for r in rows]


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


def _embed_2d(
    rows: int,
    cols: int,
    cells: tuple[list[int], list[int], list[float]],
    seed: int,
) -> Any:
    """Place `rows` sparse vectors of `cols` dimensions in the unit square so
    cosine-similar rows sit close together.

    Shared by `title_map` (a row is a title, weighted by the keywords it
    carries) and `tag_network` (a row is a tag, weighted by the titles it is
    on): both reduce the sparse matrix with truncated SVD, then UMAP with
    cosine distance, then clip outliers and scale to `[0, 1]`.
    """
    import numpy as np
    import umap
    from scipy.sparse import csr_matrix
    from sklearn.decomposition import TruncatedSVD
    from sklearn.preprocessing import normalize

    if rows < 5 or cols < 3:
        # Too few vectors for a layout to mean anything; lay them on a line.
        return np.array([[i / max(1, rows - 1), 0.5] for i in range(rows)])
    cells_r, cells_c, cells_v = cells
    matrix = csr_matrix((cells_v, (cells_r, cells_c)), shape=(rows, cols))
    matrix = normalize(matrix)
    components = min(MAP_SVD_COMPONENTS, cols - 1, rows - 1)
    reduced = TruncatedSVD(n_components=components, random_state=seed).fit_transform(matrix)
    reduced = normalize(reduced)
    coords = umap.UMAP(
        n_components=2,
        n_neighbors=min(MAP_NEIGHBOURS, rows - 1),
        metric="cosine",
        min_dist=MAP_MIN_DIST,
        spread=MAP_SPREAD,
        random_state=seed,
        n_jobs=1,  # a seed already forces one thread; saying so silences UMAP's warning
    ).fit_transform(reduced)
    low, high = np.percentile(coords, MAP_CLIP, axis=0)
    coords = np.clip(coords, low, high)
    return (coords - low) / np.where(high > low, high - low, 1.0)


def title_map(
    conn: sqlite3.Connection, kind: str, exclude: frozenset[str] = frozenset(), seed: int = 0
) -> TitleMap:
    """Every title of `kind` placed in 2D so titles with similar keywords sit close.

    A title's vector is its tags weighted by the same IDF the table shows,
    normalised to unit length so a title with forty tags is not louder than one
    with five — the cosine a keyword scorer compares. Truncated SVD takes it to
    `MAP_SVD_COMPONENTS` dimensions and UMAP (cosine distance) to two. `seed`
    fixes both, so the same store and the same exclusions draw the same map.

    numpy and scikit-learn are imported (by `_embed_2d`) rather than at the top
    of the module, so every other `plexdb` command starts without loading them.
    """
    _check_kind(kind)
    # DISTINCT so a title's tag-frequency vector below counts each keyword it
    # carries once, not once per source that happens to also list it.
    rows = conn.execute(
        "SELECT k.item_id, k.value, i.title, i.year FROM "
        "(SELECT DISTINCT item_id, value FROM enrichment WHERE namespace = ? AND key = ?) k "
        "JOIN items i USING (item_id) WHERE i.type = ?",
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
    coords = _embed_2d(len(ids), len(vocab), (cells_r, cells_c, cells_v), seed)

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


def _fingerprint(conn: sqlite3.Connection, kind: str, recipe: str) -> str:
    """A digest of what a stored default view of `kind` is drawn from.

    The recipe, then a SHA-256 over every keyword row of a title of `kind` —
    its `item_id`, source, value and `fetched_at`. Any row added, removed,
    re-stamped or re-valued, and any title changing type, changes the digest,
    including a rewrite that leaves the row count and the newest `fetched_at`
    alone. A change to the drawing code changes the recipe string.
    """
    _check_kind(kind)
    # typeshed types an aggregate as one int argument returning an int.
    conn.create_aggregate("plexdb_row_digest", 4, _RowDigest)  # type: ignore[arg-type]
    # The subquery's ORDER BY feeds the aggregate in order: SQLite does not
    # flatten an ordered subquery into an aggregating outer query.
    (digest,) = conn.execute(
        "SELECT plexdb_row_digest(item_id, source, value, fetched_at) FROM ("
        "SELECT e.item_id, e.source, e.value, e.fetched_at "
        "FROM enrichment e JOIN items i USING (item_id) "
        "WHERE e.namespace = ? AND e.key = ? AND i.type = ? "
        "ORDER BY e.item_id, e.source, e.value)",
        (_NAMESPACE, _KEY, kind),
    ).fetchone()
    return f"{recipe}|{digest}"


class _RowDigest:
    """SQL aggregate: SHA-256 over the rows it is fed, in the order fed.

    The bytes hashed are `repr` of the list of row tuples, built one row at a
    time so the rows never all sit in memory. `repr` quotes each field, so no
    two rows digest alike by moving text across a field boundary.
    """

    def __init__(self) -> None:
        self._sha = hashlib.sha256(b"[")
        self._first = True

    def step(self, *row: Any) -> None:
        if not self._first:
            self._sha.update(b", ")
        self._first = False
        self._sha.update(repr(row).encode())

    def finalize(self) -> str:
        self._sha.update(b"]")
        return self._sha.hexdigest()


def keyword_fingerprint(conn: sqlite3.Connection, kind: str) -> str:
    """A digest of what the default map of `kind` is drawn from.

    Nothing reads it except the comparison in `stored_map` and
    `plexdb.titlemap`.
    """
    return _fingerprint(conn, kind, MAP_RECIPE)


def network_fingerprint(conn: sqlite3.Connection, kind: str) -> str:
    """A digest of what the default tag network of `kind` is drawn from.

    Nothing reads it except the comparison in `stored_network` and
    `plexdb.tagnetwork`.
    """
    return _fingerprint(conn, kind, NETWORK_RECIPE)


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


#: A tag on fewer titles than this cannot draw a meaningful edge or earn a
#: position of its own, so the whole-network view never offers a threshold
#: below it — the browser's slider only hides nodes computed at this floor.
NETWORK_MIN_DF = 3

#: Each node keeps only its strongest links, so a tag central to the library
#: does not draw a line to every tag it has ever shared one title with.
NETWORK_EDGES_PER_NODE = 6

#: Names how a network is drawn, the same way `MAP_RECIPE` names the title
#: map's recipe: part of a stored network's fingerprint, so changing the
#: shared embedding recipe or either constant above makes every stored
#: network stale and the next refresh redraws it.
NETWORK_RECIPE = (
    f"umap-cosine-svd{MAP_SVD_COMPONENTS}-df{NETWORK_MIN_DF}-nn{MAP_NEIGHBOURS}"
    f"-md{MAP_MIN_DIST}-sp{MAP_SPREAD}-clip{MAP_CLIP[0]:g}-{MAP_CLIP[1]:g}"
    f"-epn{NETWORK_EDGES_PER_NODE}"
)


@dataclass(frozen=True)
class NetworkNode:
    value: str
    df: int
    x: float
    y: float


@dataclass(frozen=True)
class TagNetwork:
    kind: str
    #: Every tag of `kind` on at least `NETWORK_MIN_DF` titles, positioned so
    #: tags carried by the same titles sit close together — most titles first,
    #: ties by name.
    nodes: tuple[NetworkNode, ...]
    #: `(a, b, shared titles)`, `a < b`, each pair once, capped at
    #: `NETWORK_EDGES_PER_NODE` per node.
    edges: tuple[tuple[str, str, int], ...]


@dataclass(frozen=True)
class _NetworkVocab:
    """The first stage of `tag_network`: which tags qualify, and which titles
    carry them. `together` is per-tag co-occurrence counts, not yet capped to
    `edges_per_node` — that happens in `_network_edges`.
    """

    df: Counter[str]
    vocab: dict[str, int]
    by_title: dict[str, list[str]]
    titles: dict[str, int]
    cells: tuple[list[int], list[int], list[float]]
    together: defaultdict[str, Counter[str]]


def _network_vocab(
    conn: sqlite3.Connection, kind: str, exclude: frozenset[str], min_df: int
) -> _NetworkVocab:
    # DISTINCT so a title's tag set below counts each keyword it carries once,
    # not once per source that happens to also list it.
    rows = conn.execute(
        "SELECT k.item_id, k.value FROM "
        "(SELECT DISTINCT item_id, value FROM enrichment WHERE namespace = ? AND key = ?) k "
        "JOIN items i USING (item_id) WHERE i.type = ?",
        (_NAMESPACE, _KEY, kind),
    ).fetchall()
    df = Counter(value for _, value in rows)
    vocab = {
        value: col
        for col, value in enumerate(
            sorted(v for v, c in df.items() if c >= min_df and v not in exclude)
        )
    }

    by_title: defaultdict[str, list[str]] = defaultdict(list)
    for item_id, value in rows:
        if value in vocab:
            by_title[item_id].append(value)
    titles = {item_id: col for col, item_id in enumerate(sorted(by_title))}

    cells_r: list[int] = []
    cells_c: list[int] = []
    cells_v: list[float] = []
    together: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for item_id, values in by_title.items():
        col = titles[item_id]
        for a in values:
            cells_r.append(vocab[a])
            cells_c.append(col)
            cells_v.append(1.0)
            counts = together[a]
            for b in values:
                if a != b:
                    counts[b] += 1

    return _NetworkVocab(
        df=df,
        vocab=vocab,
        by_title=by_title,
        titles=titles,
        cells=(cells_r, cells_c, cells_v),
        together=together,
    )


def _network_edges(vocab: _NetworkVocab, edges_per_node: int) -> tuple[tuple[str, str, int], ...]:
    kept: set[tuple[str, str, int]] = set()
    for value in vocab.vocab:
        ranked = sorted(vocab.together[value].items(), key=lambda kv: (-kv[1], kv[0]))[
            :edges_per_node
        ]
        for other, count in ranked:
            a, b = sorted((value, other))
            kept.add((a, b, count))
    return tuple(sorted(kept, key=lambda e: (-e[2], e[0], e[1])))


def _network_nodes(vocab: _NetworkVocab, seed: int) -> tuple[NetworkNode, ...]:
    coords = _embed_2d(len(vocab.vocab), len(vocab.titles), vocab.cells, seed)
    values = sorted(vocab.vocab, key=vocab.vocab.__getitem__)
    return tuple(
        NetworkNode(
            value=value,
            df=vocab.df[value],
            x=round(float(coords[i][0]), 5),
            y=round(float(coords[i][1]), 5),
        )
        for i, value in enumerate(values)
    )


def tag_network(
    conn: sqlite3.Connection,
    kind: str,
    exclude: frozenset[str] = frozenset(),
    min_df: int = NETWORK_MIN_DF,
    edges_per_node: int = NETWORK_EDGES_PER_NODE,
    seed: int = 0,
) -> TagNetwork:
    """The whole tag network of `kind`: every tag on at least `min_df` titles,
    positioned by title co-membership and linked to its strongest co-tags.

    A tag's vector is which titles carry it — the transpose of `title_map`'s
    title-by-tag matrix — reduced by the same truncated-SVD-then-UMAP recipe
    (`_embed_2d`), so two tags carried by the same titles sit close together.
    Computed once at `min_df`, the widest the page's slider allows: raising the
    slider only hides nodes and edges client-side, so it never re-fetches or
    re-lays-out the network.
    """
    _check_kind(kind)
    vocab = _network_vocab(conn, kind, exclude, min_df)
    edges = _network_edges(vocab, edges_per_node)
    nodes = _network_nodes(vocab, seed)
    return TagNetwork(kind=kind, nodes=nodes, edges=edges)


def tag_network_streaming(
    conn: sqlite3.Connection,
    kind: str,
    exclude: frozenset[str],
    emit: Callable[[dict[str, object]], None],
    min_df: int = NETWORK_MIN_DF,
    edges_per_node: int = NETWORK_EDGES_PER_NODE,
    seed: int = 0,
) -> dict[str, object]:
    """Like `tag_network`, but calls `emit` with each of its first two real
    stages as it completes — filtered vocab (fast), then co-occurrence edges —
    and returns the third, the laid-out nodes (the slow SVD+UMAP step), as the
    `"stage": "done"` payload rather than emitting it: the caller's
    `_KeyedCache.get_streaming` emits the finished value itself, on every path.
    """
    _check_kind(kind)
    vocab = _network_vocab(conn, kind, exclude, min_df)
    emit(
        {
            "stage": "vocab",
            "nodes": [[value, vocab.df[value]] for value in sorted(vocab.vocab)],
        }
    )
    edges = _network_edges(vocab, edges_per_node)
    emit({"stage": "edges", "edges": [[a, b, n] for a, b, n in edges]})
    nodes = _network_nodes(vocab, seed)
    payload: dict[str, object] = {
        "stage": "done",
        "kind": kind,
        "nodes": [[n.value, n.df, n.x, n.y] for n in nodes],
        "edges": [[a, b, n] for a, b, n in edges],
    }
    return payload


def stored_network(conn: sqlite3.Connection, kind: str) -> dict[str, object] | None:
    """The default tag network the writer stored — the same `kind`/`nodes`/`edges`
    shape `tag_network_streaming`'s final stage returns — or `None` when there is
    none or it was drawn from different keyword rows.

    `None` is the caller's cue to draw it live. A store that predates the
    `tag_network` tables has neither, and reads as having no stored network.
    """
    _check_kind(kind)
    try:
        state = conn.execute(
            "SELECT fingerprint FROM tag_network_state WHERE kind = ?", (kind,)
        ).fetchone()
        if state is None or state[0] != network_fingerprint(conn, kind):
            return None
        nodes = conn.execute(
            "SELECT value, df, x, y FROM tag_network WHERE kind = ? ORDER BY value", (kind,)
        ).fetchall()
        edges = conn.execute(
            "SELECT a, b, shared FROM tag_network_edge WHERE kind = ? ORDER BY shared DESC, a, b",
            (kind,),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    return {
        "kind": kind,
        "nodes": [[r[0], r[1], r[2], r[3]] for r in nodes],
        "edges": [[r[0], r[1], r[2]] for r in edges],
    }


#: Region labels come from a k-d split of the map: depth `d` has up to `2**d`
#: regions, holding about the same number of titles each, so a dense part of the
#: map splits into many small regions and an empty part stays one. Labels start at
#: depth 2 (four regions) and stop at depth 9 (512), or where a region has fewer
#: than `LABEL_MIN_TITLES` titles.
LABEL_FIRST_DEPTH = 2
LABEL_LAST_DEPTH = 9
LABEL_MIN_TITLES = 8


class RegionLabel(NamedTuple):
    #: The region's heap number: the children of `i` are `2i+1` and `2i+2`.
    node: int
    #: Median of the region's titles, and the span of the middle 80% of them,
    #: in map units.
    x: float
    y: float
    width: float
    height: float
    titles: int
    #: The readable spelling to draw, and the stored keyword it stands for.
    text: str
    tag: str


def readable_forms(conn: sqlite3.Connection) -> dict[str, str]:
    """For each stored (stemmed) keyword, the raw spelling to show a person.

    `keyword_forms` keeps every raw spelling that normalised to a stored
    keyword but not how often a source used it, so the choice is the shortest
    spelling, ties by name. A stored keyword with no entry, or a store that
    predates the table, is absent, and the caller shows the stored form.
    """
    try:
        rows = conn.execute("SELECT surface, keyword FROM keyword_forms").fetchall()
    except sqlite3.OperationalError:
        return {}
    best: dict[str, str] = {}
    for surface, keyword in rows:
        held = best.get(keyword)
        if held is None or (len(surface), surface) < (len(held), held):
            best[keyword] = surface
    return best


def region_labels(
    conn: sqlite3.Connection,
    kind: str,
    points: Sequence[tuple[str, float, float]],
    exclude: frozenset[str] = frozenset(),
) -> list[RegionLabel]:
    """Tag labels for the regions of a drawn map, coarse to fine.

    `points` are `(item_id, x, y)`.

    Regions are the nodes of a k-d split (see `LABEL_FIRST_DEPTH`). A region
    is named by the tag with the highest count-in-region times IDF, the weight
    the map itself uses, so a tag common everywhere does not name every region.
    A tag that already names an ancestor is skipped, so each zoom level brings
    new words. Tags in `exclude`, and tags the map left out of its vectors
    (fewer than `MAP_MIN_DF` titles), never label anything.

    A `RegionLabel` serialises as a JSON array in field order.
    """
    _check_kind(kind)
    rows = conn.execute(
        "SELECT k.item_id, k.value FROM "
        "(SELECT DISTINCT item_id, value FROM enrichment WHERE namespace = ? AND key = ?) k "
        "JOIN items i USING (item_id) WHERE i.type = ?",
        (_NAMESPACE, _KEY, kind),
    ).fetchall()
    df = Counter(r[1] for r in rows)
    n = len({r[0] for r in rows})
    tags: dict[str, list[str]] = defaultdict(list)
    for item_id, value in rows:
        if df[value] >= MAP_MIN_DF and value not in exclude:
            tags[item_id].append(value)
    forms = readable_forms(conn)

    def span(values: list[float]) -> tuple[float, float]:
        values = sorted(values)
        return values[len(values) // 2], (
            values[int(0.9 * (len(values) - 1))] - values[int(0.1 * (len(values) - 1))]
        )

    labels: list[RegionLabel] = []
    # (heap number, indexes into `points`, tags already naming an ancestor)
    stack: list[tuple[int, list[int], frozenset[str]]] = [
        (0, list(range(len(points))), frozenset())
    ]
    while stack:
        node, members, used = stack.pop()
        depth = (node + 1).bit_length() - 1
        if depth > LABEL_LAST_DEPTH or len(members) < LABEL_MIN_TITLES:
            continue
        if depth >= LABEL_FIRST_DEPTH:
            counts: Counter[str] = Counter()
            for m in members:
                counts.update(tags.get(points[m][0], ()))
            floor = max(2, 0.05 * len(members))
            scored = [
                (count * (1.0 + math.log(n / df[tag])), tag)
                for tag, count in counts.items()
                if count >= floor and tag not in used
            ]
            if scored:
                tag = min(scored, key=lambda s: (-s[0], s[1]))[1]
                x, width = span([points[m][1] for m in members])
                y, height = span([points[m][2] for m in members])
                labels.append(
                    RegionLabel(
                        node,
                        round(x, 4),
                        round(y, 4),
                        round(width, 4),
                        round(height, 4),
                        len(members),
                        forms.get(tag, tag),
                        tag,
                    )
                )
                used = used | {tag}
        xs = [points[m][1] for m in members]
        ys = [points[m][2] for m in members]
        axis = 1 if max(xs) - min(xs) >= max(ys) - min(ys) else 2
        ordered = sorted(members, key=lambda m: (points[m][axis], points[m][0]))
        half = len(ordered) // 2
        stack.append((2 * node + 1, ordered[:half], used))
        stack.append((2 * node + 2, ordered[half:], used))
    labels.sort(key=lambda label: label.node)
    return labels


def title_keywords(conn: sqlite3.Connection, item_id: str) -> list[str]:
    """One title's keywords, by name."""
    # DISTINCT: a keyword two sources both list on this title is one keyword
    # on this title, not two entries in the list a person reads.
    rows = conn.execute(
        "SELECT DISTINCT value FROM enrichment WHERE item_id = ? AND namespace = ? AND key = ? "
        "ORDER BY value",
        (item_id, _NAMESPACE, _KEY),
    ).fetchall()
    return [r[0] for r in rows]


def title_keywords_json(conn: sqlite3.Connection, item_id: str) -> list[dict[str, str]]:
    """One title's keywords for the card: the stored form to click, search and
    count by, and a readable spelling (`readable_forms`) to show a person."""
    forms = readable_forms(conn)
    return [{"value": v, "label": forms.get(v, v)} for v in title_keywords(conn, item_id)]


#: How many similar titles one card lists, and the longest item id a lookup takes.
SIMILAR_LIMIT = 12
MAX_ITEM_ID = 200


class NoSuchTitle(LookupError):
    """No item has the id a card was asked for."""


class TooManyTitleRequests(Exception):
    """More /api/title or /api/titles requests are already running than TITLE_SLOTS allows."""


def title_details(conn: sqlite3.Connection, item_id: str) -> dict[str, object]:
    """What a title's card shows: its facts, its keywords, and its nearest similar titles.

    The similar titles are the `tmdb_similar` edges out of it, in the rank TMDB
    gave them; an edge's target is always an item in the store.
    """
    row = conn.execute(
        "SELECT title, year, studio, content_rating FROM items WHERE item_id = ?", (item_id,)
    ).fetchone()
    if row is None:
        raise NoSuchTitle(item_id)
    similar = conn.execute(
        "SELECT i.item_id, i.title, i.year, e.rank FROM edges e "
        "JOIN items i ON i.item_id = e.to_id "
        "WHERE e.from_id = ? AND e.edge_type = ? ORDER BY e.rank, i.title LIMIT ?",
        (item_id, SIMILAR_EDGE_TYPE, SIMILAR_LIMIT),
    ).fetchall()
    return {
        "item_id": item_id,
        "title": row["title"],
        "year": row["year"],
        "studio": row["studio"],
        "content_rating": row["content_rating"],
        "keywords": title_keywords_json(conn, item_id),
        "similar": [
            {"item_id": r["item_id"], "title": r["title"], "year": r["year"], "rank": r["rank"]}
            for r in similar
        ],
    }


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
        """`get_streaming` with a single stage: `compute` emits nothing along
        the way, and the finished value it is handed is returned."""
        result: list[dict[str, object]] = []
        self.get_streaming(key, generation, lambda _emit: compute(), result.append)
        return result[0]

    def get_streaming(
        self,
        key: object,
        generation: object,
        stages: Callable[[Callable[[dict[str, object]], None]], dict[str, object]],
        emit: Callable[[dict[str, object]], None],
    ) -> None:
        """Hand `emit` the value for `key` at `generation`, exactly once, on
        every path — a cache hit, a waiter behind another caller's compute,
        or the winner that computes it. Only the winner calls `stages(emit)`,
        which may emit intermediate results as they complete and returns the
        finished value without emitting it; a waiter never sees the
        intermediate stages. `emit` always runs outside `self._guard` — it
        does I/O for a caller streaming to an HTTP response, and holding the
        guard through that would stall every other key's request behind one
        slow socket.
        """
        while True:
            with self._guard:
                hit = self._entries.get(key)
                cached = hit[1] if hit is not None and hit[0] == generation else None
                if cached is None:
                    flight = self._flights.get((key, generation))
                    if flight is None:
                        flight = self._flights[(key, generation)] = threading.Event()
                        mine = True
                    else:
                        mine = False
            if cached is not None:
                emit(cached)
                return
            assert flight is not None
            if not mine:
                flight.wait()
                continue
            try:
                value = stages(emit)
                with self._guard:
                    self._entries.pop(key, None)
                    self._entries[key] = (generation, value)
                    while len(self._entries) > self._keep:
                        del self._entries[next(iter(self._entries))]
            finally:
                with self._guard:
                    del self._flights[(key, generation)]
                flight.set()
            emit(value)
            return


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
                rows: list[list[Any]] = payload["points"]  # type: ignore[assignment]
                placed = [(p[0], p[3], p[4]) for p in rows]
                payload = {**payload, "labels": region_labels(conn, kind, placed, exclude)}
            return payload

        return self._cache.get((kind, exclude), (stat.st_mtime_ns, stat.st_size), compute)


class _TagNetworkCache:
    """Computed tag networks, keyed on kind and exclusions, per store file stamp.

    Same shape as `_MapCache`: laying out the whole network costs as much as
    the title map does, so a repeat of the same request must not pay again, and
    a per-key lock keeps two browser tabs from computing the same network at
    once.
    """

    KEEP = 6

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._cache = _KeyedCache(self.KEEP)

    def get_streaming(
        self, kind: str, exclude: frozenset[str], emit: Callable[[dict[str, object]], None]
    ) -> None:
        """Like `get`, but a cache miss on an excluded kind streams the vocab
        and edges stages `tag_network_streaming` emits, then the "done" layout
        `_KeyedCache.get_streaming` emits, rather than blocking until the
        whole (slow) layout is done. A cache hit — the precomputed
        default network, or a network this call already laid out — always
        emits exactly one `"stage": "done"` line, so the frontend's NDJSON
        reader has one shape to handle regardless of which path served it.
        """
        stat = self._path.stat()

        def stages(emit_stage: Callable[[dict[str, object]], None]) -> dict[str, object]:
            with open_readonly(self._path) as conn:
                if not exclude:
                    stored = stored_network(conn, kind)
                    if stored is not None:
                        return {"stage": "done", **stored}
                return tag_network_streaming(conn, kind, exclude, emit_stage)

        self._cache.get_streaming((kind, exclude), (stat.st_mtime_ns, stat.st_size), stages, emit)


#: The Query tab's limits. Plex TVX has no login, locally or deployed, so
#: these hold for every caller: a statement that runs past `QUERY_SECONDS` is
#: aborted, and a result past `QUERY_ROWS` rows is cut and says so.
QUERY_SECONDS = 5.0
QUERY_ROWS = 500

#: Longest string or blob one SQL function may build (`SQLITE_LIMIT_LENGTH`). The
#: progress handler cannot interrupt a single call such as `randomblob(9e8)`, and
#: Plex TVX shares a process with the sweep, so an oversize value is refused
#: instead of allocated.
QUERY_VALUE_BYTES = 8 << 20

#: Statements that may run at once. The server answers each request on its own
#: thread; a third caller is told to retry rather than queued.
QUERY_SLOTS = threading.BoundedSemaphore(2)

#: Concurrent /api/title and /api/titles requests. Both open a fresh
#: read-only connection per request with no cache layer, unlike /api/tags,
#: /api/map and /api/tagnetwork (already single-flighted per key by
#: `_KeyedCache`) and unlike /api/poster (`POSTER_SLOTS` guards an external
#: Plex fetch, not the store). Every real UI path fires at most one of either
#: at a time — `titleDetail`'s `detailCache` and a tag click both dedupe
#: client-side — so this only needs to sit comfortably above 1: it bounds an
#: anonymous caller hammering the route directly, not a browser page load
#: (contrast `POSTER_SLOTS`, sized against a real page requesting 8+ posters
#: at once).
TITLE_SLOTS = threading.BoundedSemaphore(4)

#: Largest request body Plex TVX reads. A pasted query is a few kilobytes.
#: `SavedQueries.MAX_TOTAL_BYTES` shares this literal by coincidence, not by
#: reference — one bounds a single request, the other the whole persisted
#: file — so change either without assuming the other should follow.
MAX_BODY = 1 << 20

#: What a statement may do. Everything else — ATTACH, PRAGMA, every write and
#: every DDL — meets the authorizer's refusal, on top of the read-only
#: connection and `PRAGMA query_only`.
_ALLOWED_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
)


class QueryError(ValueError):
    """A query the store refused or could not finish; the message is SQLite's own."""


class TooManyQueries(Exception):
    """Both QUERY_SLOTS are already running a statement."""


def _deny_all_but_reads(action: int, *_: object) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def _cell(value: object) -> object:
    if isinstance(value, bytes):
        return f"<blob {len(value)} bytes>"
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)  # JSON has no inf
    return value


#: What an `item_id` starts with: each namespace `derive_item_id` ranks, then `fs:`.
_ID_PREFIXES: tuple[str, ...] = (*(f"{ns}:" for ns in PRIORITY), "fs:")

#: Ids per lookup statement, under SQLite's 999-variable limit on older builds.
_LABEL_CHUNK = 500


def _item_label(
    kind: str,
    title: str,
    show_title: str | None,
    season: int | None,
    episode: int | None,
    year: int | None,
) -> str:
    if kind == "episode" and show_title:
        code = f" S{season:02d}E{episode:02d}" if season is not None and episode is not None else ""
        return f"{show_title}{code}: {title}"
    return f"{title} ({year})" if year is not None else title


def _item_labels(conn: sqlite3.Connection, rows: list[list[object]]) -> dict[str, str]:
    """A readable label for each item id found among the result cells.

    One SELECT per chunk of ids on the query's own connection. An id with no
    `items` row is left out, and a cell that is not id-shaped is never looked up.
    """
    ids = sorted(
        {c for row in rows for c in row if isinstance(c, str) and c.startswith(_ID_PREFIXES)}
    )
    labels: dict[str, str] = {}
    for start in range(0, len(ids), _LABEL_CHUNK):
        chunk = ids[start : start + _LABEL_CHUNK]
        marks = ",".join("?" * len(chunk))
        for item_id, *fields in conn.execute(
            "SELECT item_id, type, title, show_title, season, episode, year "
            f"FROM items WHERE item_id IN ({marks})",
            chunk,
        ):
            labels[item_id] = _item_label(*fields)
    return labels


def run_query(
    store_path: Path, sql: str, *, seconds: float = QUERY_SECONDS, rows: int = QUERY_ROWS
) -> dict[str, object]:
    """Run one SELECT against a fresh read-only connection and return its rows.

    Raises `QueryError` for anything SQLite refuses: a syntax error, a second
    statement, an action the authorizer denies, or a run past `seconds`, and
    `TooManyQueries` when both QUERY_SLOTS are taken.
    """
    if not sql.strip():
        raise QueryError("no SQL to run")
    if not QUERY_SLOTS.acquire(blocking=False):
        raise TooManyQueries("two queries are already running; try again in a moment")
    try:
        return _run_query(store_path, sql, seconds, rows)
    finally:
        QUERY_SLOTS.release()


def _configure_query_connection(conn: sqlite3.Connection) -> None:
    """The PRAGMAs every Query tab statement runs under, before the authorizer
    goes on — so this connection can still set them itself.

    `WITH RECURSIVE ... ORDER BY` (plex-db-ex-oyg.4) can't stream: SQLite sorts
    the whole result before yielding a row. Measured against the deploy base
    image (`python:3.12-slim`): the default `temp_store` already spills that
    sort to a temp file and peaks at 26 MB RSS over the full `QUERY_SECONDS`
    window, but that's an unstated dependency on the linked SQLite's
    compiled default — forcing MEMORY instead peaks at 864 MB in the same
    window. Pinning `FILE` here makes the 26 MB bound a guarantee rather than
    an accident of what this build ships.
    """
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA temp_store = FILE")
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, QUERY_VALUE_BYTES)


def _run_query(store_path: Path, sql: str, seconds: float, rows: int) -> dict[str, object]:
    started = time.monotonic()
    deadline = started + seconds
    timed_out = False

    def past_deadline() -> int:
        nonlocal timed_out
        timed_out = time.monotonic() > deadline
        return 1 if timed_out else 0

    with open_readonly(store_path) as conn:
        try:
            _configure_query_connection(conn)
            conn.set_authorizer(_deny_all_but_reads)
            conn.set_progress_handler(past_deadline, 1000)
            cursor = conn.execute(sql)
            if cursor.description is None:
                raise QueryError("the statement returns no rows")
            columns = [d[0] for d in cursor.description]
            fetched = cursor.fetchmany(rows + 1)
            shown = [[_cell(v) for v in row] for row in fetched[:rows]]
            # The user's statement finished inside its budget; the label lookup is
            # decoration and must not turn that result into a timeout.
            conn.set_progress_handler(None, 0)
            labels = _item_labels(conn, shown)
        except sqlite3.Error as err:
            if timed_out:
                raise QueryError(f"query stopped after {seconds:g} seconds") from err
            raise QueryError(str(err)) from err
    return {
        "columns": columns,
        "rows": shown,
        "labels": labels,
        "row_count": min(len(fetched), rows),
        "truncated": len(fetched) > rows,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }


#: Read-only queries the Query tab offers as starting points. The first group
#: orients in the schema; the second is the SQL behind each accessor of the
#: `plexdb-reader` crate, with the values it binds written in as literals to edit.
RECIPES: tuple[dict[str, str], ...] = (
    {
        "group": "Orientation",
        "name": "Row counts per table",
        "sql": (
            "SELECT 'items' AS tbl, COUNT(*) AS n FROM items\n"
            "UNION ALL SELECT 'plays', COUNT(*) FROM plays\n"
            "UNION ALL SELECT 'enrichment', COUNT(*) FROM enrichment\n"
            "UNION ALL SELECT 'edges', COUNT(*) FROM edges\n"
            "UNION ALL SELECT 'collection', COUNT(*) FROM collection\n"
            "UNION ALL SELECT 'collection_membership', COUNT(*) FROM collection_membership\n"
            "UNION ALL SELECT 'external_ids', COUNT(*) FROM external_ids\n"
            "UNION ALL SELECT 'plex_items', COUNT(*) FROM plex_items"
        ),
    },
    {
        "group": "Orientation",
        "name": "Titles per type",
        "sql": "SELECT type, COUNT(*) AS titles FROM items GROUP BY type ORDER BY titles DESC",
    },
    {
        "group": "Orientation",
        "name": "Top enrichment keys per namespace",
        "sql": (
            "SELECT namespace, key, COUNT(*) AS rows_, COUNT(DISTINCT item_id) AS titles,\n"
            "       COUNT(DISTINCT value) AS distinct_values\n"
            "FROM enrichment\n"
            "GROUP BY namespace, key\n"
            "ORDER BY rows_ DESC\n"
            "LIMIT 50"
        ),
    },
    {
        "group": "Orientation",
        "name": "Plays per account",
        "sql": (
            "SELECT plex_account_id, COUNT(*) AS plays, MIN(viewed_at) AS first_play,\n"
            "       MAX(viewed_at) AS last_play\n"
            "FROM plays\n"
            "GROUP BY plex_account_id\n"
            "ORDER BY plays DESC"
        ),
    },
    {
        "group": "Reader accessors",
        "name": "enrichment_for: one title, one namespace",
        "sql": (
            "SELECT namespace, key, value, fetched_at\n"
            "FROM enrichment\n"
            "WHERE item_id = 'imdb:tt1375666' AND namespace = 'keywords'\n"
            "ORDER BY key, value"
        ),
    },
    {
        "group": "Reader accessors",
        "name": "enrichment_for_many: several titles, one namespace",
        "sql": (
            "SELECT e.item_id, i.title, i.year, e.namespace, e.key, e.value, e.fetched_at\n"
            "FROM enrichment e\n"
            "LEFT JOIN items i ON i.item_id = e.item_id\n"
            "WHERE e.item_id IN ('imdb:tt1375666', 'imdb:tt0133093')\n"
            "  AND e.namespace = 'keywords'\n"
            "ORDER BY e.item_id, e.key, e.value"
        ),
    },
    {
        "group": "Reader accessors",
        "name": "edges_from: what a title points at",
        "sql": (
            "SELECT e.from_id, f.title AS from_title, e.to_id, t.title AS to_title,\n"
            "       t.year AS to_year,\n"
            "       e.edge_type, e.rank, e.fetched_at\n"
            "FROM edges e\n"
            "LEFT JOIN items f ON f.item_id = e.from_id\n"
            "LEFT JOIN items t ON t.item_id = e.to_id\n"
            "WHERE e.from_id = 'imdb:tt1375666' AND e.edge_type = 'tmdb_recommendations'\n"
            "ORDER BY e.rank"
        ),
    },
    {
        "group": "Reader accessors",
        "name": "edges_to: what points at a title",
        "sql": (
            "SELECT e.from_id, f.title AS from_title, f.year AS from_year, e.to_id,\n"
            "       t.title AS to_title,\n"
            "       e.edge_type, e.rank, e.fetched_at\n"
            "FROM edges e\n"
            "LEFT JOIN items f ON f.item_id = e.from_id\n"
            "LEFT JOIN items t ON t.item_id = e.to_id\n"
            "WHERE e.to_id = 'imdb:tt1375666' AND e.edge_type = 'tmdb_recommendations'\n"
            "ORDER BY e.rank"
        ),
    },
    {
        "group": "Reader accessors",
        "name": "collections_for: a title's collections",
        "sql": (
            "SELECT cm.collection_id, c.source, c.name, c.url, c.size, c.likes,\n"
            "       cm.rank, cm.mentions, cm.observed_at\n"
            "FROM collection_membership cm\n"
            "JOIN collection c ON c.collection_id = cm.collection_id\n"
            "WHERE cm.item_id = 'imdb:tt1375666'\n"
            "ORDER BY cm.collection_id"
        ),
    },
    {
        "group": "Reader accessors",
        "name": "taste_vector_for: plays per unit, one account",
        "sql": (
            "SELECT COALESCE(i.show_item_id, p.item_id) AS unit, u.title, u.year,\n"
            "       COUNT(*) AS plays\n"
            "FROM plays p\n"
            "JOIN items i ON i.item_id = p.item_id\n"
            "LEFT JOIN items u ON u.item_id = COALESCE(i.show_item_id, p.item_id)\n"
            "WHERE p.plex_account_id = 1\n"
            "GROUP BY unit\n"
            "ORDER BY unit"
        ),
    },
    {
        "group": "Reader accessors",
        "name": "watched_units_for: one account's watched units",
        "sql": (
            "SELECT DISTINCT COALESCE(i.show_item_id, p.item_id) AS unit, u.title, u.year\n"
            "FROM plays p\n"
            "JOIN items i ON i.item_id = p.item_id\n"
            "LEFT JOIN items u ON u.item_id = COALESCE(i.show_item_id, p.item_id)\n"
            "WHERE p.plex_account_id = 1\n"
            "ORDER BY unit"
        ),
    },
    {
        "group": "Reader accessors",
        "name": "watched_units: every account's watched units",
        "sql": (
            "SELECT DISTINCT COALESCE(i.show_item_id, p.item_id) AS unit, u.title, u.year\n"
            "FROM plays p\n"
            "JOIN items i ON i.item_id = p.item_id\n"
            "LEFT JOIN items u ON u.item_id = COALESCE(i.show_item_id, p.item_id)\n"
            "ORDER BY unit"
        ),
    },
    {
        "group": "Reader accessors",
        "name": "pooled_taste_vector: plays per unit, all accounts",
        "sql": (
            "SELECT COALESCE(i.show_item_id, p.item_id) AS unit, u.title, u.year,\n"
            "       COUNT(*) AS plays\n"
            "FROM plays p\n"
            "JOIN items i ON i.item_id = p.item_id\n"
            "LEFT JOIN items u ON u.item_id = COALESCE(i.show_item_id, p.item_id)\n"
            "GROUP BY unit\n"
            "ORDER BY plays DESC"
        ),
    },
    {
        "group": "Reader accessors",
        "name": "Season lengths (median-season input)",
        "sql": (
            "SELECT e.show_item_id, s.title, s.year, e.season, COUNT(*) AS episodes\n"
            "FROM items e\n"
            "LEFT JOIN items s ON s.item_id = e.show_item_id\n"
            "WHERE e.type = 'episode' AND e.show_item_id IS NOT NULL AND e.season IS NOT NULL\n"
            "GROUP BY e.show_item_id, e.season\n"
            "ORDER BY e.show_item_id, episodes"
        ),
    },
    {
        "group": "Reader accessors",
        "name": "Attributes of one title (attributes_by_item, narrowed)",
        "sql": (
            "SELECT DISTINCT e.item_id, i.title, i.year, e.namespace, e.key, e.value\n"
            "FROM enrichment e\n"
            "LEFT JOIN items i ON i.item_id = e.item_id\n"
            "WHERE e.item_id = 'imdb:tt1375666'\n"
            "ORDER BY e.item_id, e.namespace, e.key, e.value"
        ),
    },
)


class NoSuchQuery(LookupError):
    """A delete named a saved query that is not there."""


class SavedQueriesError(OSError):
    """The saved-queries file could not be read, parsed or replaced."""


class SavedQueries:
    """Named queries in one JSON file beside the store, never inside it.

    The store has one writer (ADR-0001), so what the Query tab saves lives in a
    file of its own, replaced by rename so a reader never sees half of it.
    """

    NAME_MAX = 200
    #: Plex TVX has no login, so `upsert` bounds what an anonymous caller
    #: can grow this file to — count and serialized size — independently of
    #: which directory it lives in (plex-db-ex-oyg.3).
    MAX_ENTRIES = 500
    MAX_TOTAL_BYTES = 1 << 20

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def _read(self) -> list[dict[str, str]]:
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            return []
        except ValueError as err:
            raise SavedQueriesError(f"{self._path} is not valid JSON: {err}") from err
        except OSError as err:
            raise SavedQueriesError(str(err)) from err
        if not isinstance(raw, list) or not all(
            isinstance(q, dict) and all(isinstance(q.get(k), str) for k in ("name", "sql", "note"))
            for q in raw
        ):
            raise SavedQueriesError(
                f"{self._path} does not hold a list of name, sql and note entries"
            )
        return raw

    def _write(self, queries: list[dict[str, str]]) -> None:
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(queries, indent=2) + "\n")
            tmp.replace(self._path)
        except OSError as err:
            raise SavedQueriesError(str(err)) from err
        finally:
            tmp.unlink(missing_ok=True)

    def all(self) -> list[dict[str, str]]:
        with self._lock:
            return self._read()

    def upsert(self, name: object, sql: object, note: object) -> list[dict[str, str]]:
        if not isinstance(name, str) or not name.strip() or len(name) > self.NAME_MAX:
            raise ValueError(f"name must be text of 1 to {self.NAME_MAX} characters")
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("sql must be text")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise ValueError("note must be text")
        entry = {"name": name.strip(), "sql": sql, "note": note}
        with self._lock:
            queries = [q for q in self._read() if q.get("name") != entry["name"]]
            queries.append(entry)
            if len(queries) > self.MAX_ENTRIES:
                raise ValueError(f"too many saved queries (max {self.MAX_ENTRIES})")
            body = json.dumps(queries, indent=2) + "\n"
            if len(body.encode()) > self.MAX_TOTAL_BYTES:
                raise ValueError(f"saved queries would exceed {self.MAX_TOTAL_BYTES} bytes")
            queries.sort(key=lambda q: q["name"].casefold())
            self._write(queries)
            return queries

    def delete(self, name: str) -> list[dict[str, str]]:
        with self._lock:
            queries = self._read()
            kept = [q for q in queries if q.get("name") != name]
            if len(kept) == len(queries):
                raise NoSuchQuery(name)
            self._write(kept)
            return kept


class NoPoster(LookupError):
    """No Plex rating key on file for this item."""


class PosterUnavailable(Exception):
    """Plex isn't configured, or the fetch itself failed (docs/adr/0017)."""


#: A poster from Plex's own `/library/metadata/<rating_key>/thumb` is a few
#: hundred KB; refuse anything wildly larger rather than buffer an unbounded
#: response from a misbehaving upstream.
POSTER_MAX_BYTES = 4 << 20

#: A cached poster answers instantly; only a *miss* reaches Plex, so this caps
#: concurrent fetches to the real server, not the route itself. A results
#: table or a tag's title list can legitimately show a couple dozen distinct
#: uncached posters at once (`loading="lazy"` still preloads a viewport's
#: worth); measured 8 concurrent misses from a single Query tab page load
#: tripping `BoundedSemaphore(4)` and showing false placeholders that only
#: cleared on a reload, so this is wide enough for one browser tab's own
#: page load, not just a lone request.
POSTER_SLOTS = threading.BoundedSemaphore(16)

#: Cached posters kept in memory, keyed by rating_key, count-capped rather
#: than time-capped — a rating_key's image doesn't change, but Plex TVX
#: has no login, so a caller cycling through many item_ids must not grow
#: this without bound (the same shape as `SavedQueries`'s caps).
POSTER_CACHE_ENTRIES = 2000


class _PosterCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[bytes, str]] = OrderedDict()

    def get(self, rating_key: str) -> tuple[bytes, str] | None:
        with self._lock:
            hit = self._entries.get(rating_key)
            if hit is not None:
                self._entries.move_to_end(rating_key)
            return hit

    def put(self, rating_key: str, data: bytes, content_type: str) -> None:
        with self._lock:
            self._entries[rating_key] = (data, content_type)
            self._entries.move_to_end(rating_key)
            while len(self._entries) > POSTER_CACHE_ENTRIES:
                self._entries.popitem(last=False)


class PosterProxy:
    """One title's Plex thumbnail, proxied so `PLEX_TOKEN` never reaches the
    browser (plex-db-ex-oyg.2). Plex TVX has no login, so a caller who
    could read the token directly could spend it against Plex at will;
    proxying keeps it server-side, and `POSTER_SLOTS`/`_PosterCache` bound how
    much of that spending an anonymous caller can cause.
    """

    def __init__(
        self,
        store_path: Path,
        plex_url: str,
        plex_token: str,
        http: httpx.Client | None = None,
    ) -> None:
        self._store_path = store_path
        self._plex_url = plex_url.rstrip("/")
        self._plex_token = plex_token
        self._cache = _PosterCache()
        self._http = http or httpx.Client(timeout=10.0)

    def get(self, item_id: str) -> tuple[bytes, str]:
        if not self._plex_url or not self._plex_token:
            raise PosterUnavailable("PLEX_URL/PLEX_TOKEN are not configured")
        with open_readonly(self._store_path) as conn:
            row = conn.execute(
                "SELECT rating_key FROM plex_items WHERE item_id = ? "
                "ORDER BY last_seen DESC LIMIT 1",
                (item_id,),
            ).fetchone()
        if row is None:
            raise NoPoster(item_id)
        rating_key = row[0]
        cached = self._cache.get(rating_key)
        if cached is not None:
            return cached
        if not POSTER_SLOTS.acquire(blocking=False):
            raise PosterUnavailable("too many poster fetches in flight; try again in a moment")
        try:
            data, content_type = self._fetch(rating_key)
        except httpx.HTTPError as err:
            raise PosterUnavailable(f"could not reach Plex: {err}") from err
        finally:
            POSTER_SLOTS.release()
        self._cache.put(rating_key, data, content_type)
        return data, content_type

    def _fetch(self, rating_key: str) -> tuple[bytes, str]:
        # Streamed rather than `.get()`, so an oversize response is refused as
        # its bytes arrive rather than after they are already fully buffered.
        with self._http.stream(
            "GET",
            f"{self._plex_url}/library/metadata/{rating_key}/thumb",
            headers={"X-Plex-Token": self._plex_token},
        ) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "image/jpeg")
            chunks = bytearray()
            for chunk in resp.iter_bytes():
                chunks += chunk
                if len(chunks) > POSTER_MAX_BYTES:
                    raise PosterUnavailable(f"poster is over {POSTER_MAX_BYTES} bytes")
        return bytes(chunks), content_type


#: The fields a decision POST carries, in the order `MergeDecisions.check` takes them.
MERGE_FIELDS = ("keyword_a", "keyword_b", "decision")
#: The same for `/api/roles` and `RoleDecisions.check`.
ROLE_FIELDS = ("keyword", "role", "decision")


def merges_json(pairs: list[dict[str, object]]) -> dict[str, object]:
    """The review page's two lists: the queue, best score first, and the decided or merged
    pairs, a person's latest decision first and Jev-confirmed pairs after, best score first."""

    def score(p: dict[str, object]) -> float:
        return cast(float, p["score"])

    proposed = sorted(
        (p for p in pairs if p["bucket"] == "proposed"),
        key=lambda p: (-score(p), str(p["keyword_a"]), str(p["keyword_b"])),
    )
    by_person = sorted(
        (p for p in pairs if p["decision"] is not None),
        key=lambda p: (str(p["decided_at"]), score(p), str(p["keyword_a"]), str(p["keyword_b"])),
        reverse=True,
    )
    by_jev = sorted(
        (p for p in pairs if p["decision"] is None and p["bucket"] == "merged"),
        key=lambda p: (-score(p), str(p["keyword_a"]), str(p["keyword_b"])),
    )
    return {"proposed": proposed, "decided": by_person + by_jev}


def _page() -> bytes:
    return resources.files("plexdb").joinpath("explore.html").read_bytes()


def _error_reply(err: Exception, store_path: Path) -> tuple[str, HTTPStatus] | None:
    """The message and status an `/api/*` route answers with for `err`, or
    None for a failure no route expects — that one stays a traceback.

    One table for both reply shapes: a JSON route sends the pair as its
    status and body, and the NDJSON tag-network stream, whose 200 is already
    on the wire, sends the message alone as its "error" line.
    """
    if isinstance(err, TooManyQueries):
        return str(err), HTTPStatus.TOO_MANY_REQUESTS
    if isinstance(err, ValueError):
        return str(err), HTTPStatus.BAD_REQUEST
    if isinstance(err, NoSuchTitle):
        return f"no title with item_id {err.args[0]!r}", HTTPStatus.NOT_FOUND
    if isinstance(err, NoSuchPair):
        return f"no keyword pair {err.args[0]!r} in the store", HTTPStatus.NOT_FOUND
    if isinstance(err, NoSuchRole):
        return f"no keyword role {err.args[0]!r} in the store", HTTPStatus.NOT_FOUND
    if isinstance(err, NoSuchQuery):
        return f"no saved query named {err.args[0]!r}", HTTPStatus.NOT_FOUND
    if isinstance(err, TooManyTitleRequests):
        return str(err), HTTPStatus.TOO_MANY_REQUESTS
    if isinstance(err, NoPoster):
        return f"no Plex rating key on file for {err.args[0]!r}", HTTPStatus.NOT_FOUND
    if isinstance(err, PosterUnavailable):
        return str(err), HTTPStatus.SERVICE_UNAVAILABLE
    if isinstance(err, FileNotFoundError):
        # On the host this is the normal state until the first sweep
        # publishes a snapshot, not a crash worth a traceback.
        return (
            f"no store at {store_path} yet — the next sweep publishes one",
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
    if isinstance(err, StoreError):
        return str(err), HTTPStatus.SERVICE_UNAVAILABLE
    if isinstance(err, sqlite3.DatabaseError) and not isinstance(err, sqlite3.ProgrammingError):
        # ProgrammingError (a closed connection, a wrong parameter count) is a `DatabaseError`
        # and InterfaceError is not; both are code bugs and keep their
        # traceback. A user's own SQL never reaches here: run_query turns it
        # into a QueryError.
        return f"could not read the store: {err}", HTTPStatus.SERVICE_UNAVAILABLE
    if isinstance(err, SavedQueriesError):
        return f"saved queries: {err}", HTTPStatus.INTERNAL_SERVER_ERROR
    if isinstance(err, DecisionsFileError):
        return f"decisions file: {err}", HTTPStatus.INTERNAL_SERVER_ERROR
    if isinstance(err, OSError):
        return f"could not read the store: {err}", HTTPStatus.INTERNAL_SERVER_ERROR
    return None


def _log_store_damage(err: Exception) -> None:
    """One stderr line for a failure that means the store itself is unhealthy.

    "No store yet", the saved-queries file and a client that hung up
    mid-reply (`ConnectionError`) are normal or unrelated states, and a code
    bug keeps its traceback, so none of them logs here.
    """
    if isinstance(err, (StoreError, sqlite3.DatabaseError, OSError)) and not isinstance(
        err,
        (
            FileNotFoundError,
            ConnectionError,
            SavedQueriesError,
            DecisionsFileError,
            sqlite3.ProgrammingError,
        ),
    ):
        print(f"explore: store problem: {type(err).__name__}: {err}", file=sys.stderr)


def make_server(
    store_path: Path,
    host: str,
    port: int,
    saved_path: Path | None = None,
    *,
    plex_url: str = "",
    plex_token: str = "",
    poster_http: httpx.Client | None = None,
) -> ThreadingHTTPServer:
    """An HTTP server for Plex TVX, bound but not yet serving.

    The caller owns the loop and the shutdown: `plexdb explore` runs
    `serve_forever` in the foreground, and a test runs it on a thread.
    `poster_http` is a test-only seam (an `httpx.Client` on a `MockTransport`)
    for exercising `/api/poster` without a real Plex server.
    """
    cache = _IndexCache(store_path)
    maps = _MapCache(store_path)
    networks = _TagNetworkCache(store_path)
    saved_file = saved_path or store_path.with_name(SAVED_FILE)
    saved = SavedQueries(saved_file)
    merges = MergeDecisions(saved_file.with_name(MERGE_DECISIONS_FILE))
    roles = RoleDecisions(saved_file.with_name(ROLE_DECISIONS_FILE))
    posters = PosterProxy(store_path, plex_url, plex_token, poster_http)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — the stdlib's name
            self._serve(self._get)

        def do_POST(self) -> None:  # noqa: N802
            self._serve(self._post)

        def do_PUT(self) -> None:  # noqa: N802
            self._serve(self._put)

        def do_DELETE(self) -> None:  # noqa: N802
            self._serve(self._delete)

        def _serve(self, route: Callable[[], None]) -> None:
            try:
                route()
            except Exception as err:
                reply = _error_reply(err, store_path)
                if reply is None:
                    raise
                _log_store_damage(err)
                message, status = reply
                self._json({"error": message}, status)

        def _body(self) -> dict[str, object]:
            # A page on another site can send `text/plain` without a preflight;
            # `application/json` it cannot, so requiring it keeps such a page out.
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Content-Type must be application/json")
            length = self.headers.get("Content-Length", "")
            if not length.isdigit():
                raise ValueError("a JSON body with a Content-Length is required")
            if int(length) > MAX_BODY:
                raise ValueError(f"body is over {MAX_BODY} bytes")
            try:
                body = json.loads(self.rfile.read(int(length)))
            except ValueError as err:
                raise ValueError(f"body is not JSON: {err}") from err
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            return body

        def _post(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/merges":
                body = self._body()
                a, b, decision = merges.check(*(body.get(k) for k in MERGE_FIELDS))
                # Only a pair the table holds may be decided, so an anonymous caller cannot
                # grow the file with names that were never judged.
                with open_readonly(store_path) as conn:
                    require_pair(conn, (a, b))
                    entry = merges.record((a, b), decision)
                    pair = review_rows(conn, {(a, b): entry}, readable_forms(conn), (a, b))[0]
                self._json({"pair": pair})
                return
            if path == "/api/roles":
                body = self._body()
                keyword, role, decision = roles.check(*(body.get(k) for k in ROLE_FIELDS))
                # As for a pair: only a (keyword, role) the table holds may be decided.
                with open_readonly(store_path) as conn:
                    require_role(conn, keyword, role)
                    entry = roles.record((keyword, role), decision)
                    cell = role_cell(conn, roles.latest(), readable_forms(conn), keyword, role)
                print(
                    f"explore: role decision {keyword!r} {role} {decision} -> {roles.path}",
                    file=sys.stderr,
                )
                self._json({"cell": cell, "entry": entry})
                return
            if path != "/api/query":
                self._json({"error": f"no route {self.path}"}, HTTPStatus.NOT_FOUND)
                return
            sql = self._body().get("sql")
            if not isinstance(sql, str):
                raise ValueError("sql must be text")
            self._json(run_query(store_path, sql))

        def _put(self) -> None:
            if urlparse(self.path).path != "/api/saved":
                self._json({"error": f"no route {self.path}"}, HTTPStatus.NOT_FOUND)
                return
            body = self._body()
            self._json(
                {"queries": saved.upsert(body.get("name"), body.get("sql"), body.get("note"))}
            )

        def _delete(self) -> None:
            url = urlparse(self.path)
            if url.path != "/api/saved":
                self._json({"error": f"no route {url.path}"}, HTTPStatus.NOT_FOUND)
                return
            name = parse_qs(url.query).get("name", [""])[0]
            if not name:
                raise ValueError("name is required")
            self._json({"queries": saved.delete(name)})

        def _get(self) -> None:
            url = urlparse(self.path)
            lists = parse_qs(url.query)
            query = {k: v[0] for k, v in lists.items()}
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
                if not TITLE_SLOTS.acquire(blocking=False):
                    raise TooManyTitleRequests(
                        "too many title requests are already running; try again in a moment"
                    )
                try:
                    with open_readonly(store_path) as conn:
                        self._json(titles_json(kind, tag, titles_tagged(conn, kind, tag)))
                finally:
                    TITLE_SLOTS.release()
            elif url.path == "/api/map":
                kind = query.get("kind", "movie")
                _check_kind(kind)
                self._json(maps.get(kind, frozenset(lists.get("exclude", []))))
            elif url.path == "/api/tagnetwork":
                kind = query.get("kind", "movie")
                _check_kind(kind)
                self._stream_network(kind, frozenset(lists.get("exclude", [])))
            elif url.path == "/api/saved":
                self._json({"queries": saved.all()})
            elif url.path == "/api/merges":
                with open_readonly(store_path) as conn:
                    pairs = review_rows(conn, merges.latest(), readable_forms(conn))
                self._json(merges_json(pairs))
            elif url.path == "/api/roles":
                with open_readonly(store_path) as conn:
                    self._json(
                        roles_json(conn, roles.latest(), readable_forms(conn), query.get("q", ""))
                    )
            elif url.path == "/api/recipes":
                self._json({"recipes": list(RECIPES)})
            elif url.path == "/api/title":
                item_id = query.get("item_id")
                if not item_id:
                    raise ValueError("item_id is required")
                if len(item_id) > MAX_ITEM_ID:
                    raise ValueError(f"item_id is over {MAX_ITEM_ID} characters")
                if not TITLE_SLOTS.acquire(blocking=False):
                    raise TooManyTitleRequests(
                        "too many title requests are already running; try again in a moment"
                    )
                try:
                    with open_readonly(store_path) as conn:
                        self._json(title_details(conn, item_id))
                finally:
                    TITLE_SLOTS.release()
            elif url.path == "/api/poster":
                item_id = query.get("item_id")
                if not item_id:
                    raise ValueError("item_id is required")
                if len(item_id) > MAX_ITEM_ID:
                    raise ValueError(f"item_id is over {MAX_ITEM_ID} characters")
                data, content_type = posters.get(item_id)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                # A rating_key's poster doesn't change under our feet; let the
                # browser skip the round trip entirely on a repeat view.
                self.send_header("Cache-Control", "public, max-age=604800, immutable")
                self.end_headers()
                self.wfile.write(data)
            else:
                self._json({"error": f"no route {url.path}"}, HTTPStatus.NOT_FOUND)

        def _json(self, body: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
            data = json.dumps(body, separators=(",", ":")).encode()
            self._send(status, "application/json", data)

        def _stream_network(self, kind: str, exclude: frozenset[str]) -> None:
            # One JSON object per line, flushed as each stage of `networks`
            # completes — the cached (stored or already-computed) case still
            # writes exactly one "done" line, so the frontend's NDJSON reader
            # never special-cases it. Headers go out before the compute even
            # starts, so a failure past this point becomes an "error" line
            # rather than an HTTP error status — there is no way back to a
            # fresh response once the client has already seen 200 OK.
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            broken = False

            def emit(chunk: dict[str, object]) -> None:
                # A write failure here means the client is gone — a closed
                # tab, or a kind/noise change that aborted the fetch. Swallow
                # it rather than let it escape `networks.get_streaming`: the
                # SVD+UMAP layout this call may already be most of the way
                # through is expensive, and single-flight only pays for it
                # once if the compute is allowed to run to completion and
                # land in the cache regardless of who is still listening.
                nonlocal broken
                if broken:
                    return
                try:
                    self.wfile.write(json.dumps(chunk, separators=(",", ":")).encode())
                    self.wfile.write(b"\n")
                    self.wfile.flush()
                except OSError:
                    broken = True

            try:
                networks.get_streaming(kind, exclude, emit)
            except Exception as err:
                # The same text a JSON route would send; only the status is lost.
                reply = _error_reply(err, store_path)
                if reply is None:
                    # No route expects this failure, so the browser gets only
                    # its bare text; the traceback goes where a JSON route's
                    # would, to stderr.
                    traceback.print_exception(err, file=sys.stderr)
                else:
                    _log_store_damage(err)
                message = reply[0] if reply else str(err)
            else:
                return
            try:
                emit({"stage": "error", "error": message})
            except Exception:
                pass

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


def serve_in_background(
    store_path: Path,
    host: str,
    port: int,
    saved_path: Path | None = None,
    *,
    plex_url: str = "",
    plex_token: str = "",
) -> ThreadingHTTPServer:
    """Start Plex TVX on a daemon thread and return its server.

    This is how the container serves it: a thread inside `plexdb schedule`, so
    one image and one `[docker_run]` carry both. A daemon thread dies with the
    scheduler rather than holding the container up after it, and an exception
    in one request stays in that request's own thread.
    """
    server = make_server(
        store_path, host, port, saved_path, plex_url=plex_url, plex_token=plex_token
    )
    threading.Thread(target=server.serve_forever, name="explore", daemon=True).start()
    return server
