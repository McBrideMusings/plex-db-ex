"""Fetching TMDB recommendations and similar titles into affinity edges.

Two edge types, one TMDB source, kept apart because they measure different
things: `recommendations` is behavioural ("people who engaged with this also
engaged with that"), `similar` is content-derived. A re-pull replaces the
whole `(from_id, edge_type)` set — the same wipe-and-rewrite discipline
`enrich_tmdb.py` uses for a title's keyword set — so an edge the source
dropped disappears rather than going stale.

**Only edges between two titles this store already knows are representable.**
`edges.from_id` and `edges.to_id` both reference `items`, which is populated
exclusively by `plexdb walk` (ADR-0005). A TMDB recommendation pointing at a
title this library has never walked has a TMDB id and nothing else — no
`item_id`, because the schema has no stub-item or candidate-title table for
something nobody owns. Such a result is counted in
`edges_skipped_not_in_library` and dropped, not stored under an invented id:
inventing one (e.g. `tmdb:<id>`) risks a later walk deriving a *different*
`item_id` for that same title from a stronger GUID (IMDb), leaving an orphan
edge under an id nothing else ever uses again — exactly the drift ADR-0002's
first-hit-wins ordering exists to prevent.

**Caching needs a cursor `edges` rows alone cannot supply.** A title whose
recommendations are all outside the library — or genuinely empty — leaves no
`edges` row behind, so there is nothing to read a `fetched_at` off on the next
sweep. The `tmdb_edges` enrichment namespace holds a per-`(item_id,
edge_type)` sentinel row purely for this bookkeeping, the same trick
`enrich_tmdb.py`'s `_SENTINEL_KEY` uses for a title with zero keywords. It
carries no relationship data — that lives in `edges` — and no other writer
may touch it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .errors import TMDbError
from .staleness import DEFAULT_STALE_DAYS, is_stale
from .tmdb_client import TMDbSource
from .tmdb_common import MAX_CONSECUTIVE_FAILURES, media_type_for

#: This module's two edge types. Distinct, and never merged into one.
RECOMMENDATIONS_EDGE_TYPE = "tmdb_recommendations"
SIMILAR_EDGE_TYPE = "tmdb_similar"

#: The enrichment namespace holding this module's per-title fetch cursor.
#: Not a relationship namespace — see the module docstring.
_CURSOR_NAMESPACE = "tmdb_edges"
_CURSOR_KEYS: dict[str, str] = {
    RECOMMENDATIONS_EDGE_TYPE: "_fetched_recommendations",
    SIMILAR_EDGE_TYPE: "_fetched_similar",
}
_SENTINEL_VALUE = "1"

#: A bound `TMDbSource` method returning ordered TMDB ids for one title, best
#: match first — `source.recommendations` or `source.similar`. Bound, so a
#: sweep needs the one method it was handed and never the whole source.
_Fetch = Callable[[str, str], list[str]]


@dataclass
class EdgeStats:
    """What one edge-type sweep touched."""

    titles_seen: int = 0
    titles_fetched: int = 0
    titles_cached: int = 0
    #: Movies/shows with no `tmdb` row in `external_ids` — skipped, not an
    #: error, same as `enrich_tmdb.py`.
    titles_skipped_no_tmdb_id: int = 0
    titles_failed: int = 0
    edges_written: int = 0
    #: A recommended/similar TMDB id this sweep could not turn into a stored
    #: edge: no matching item in this store (see the module docstring), or a
    #: title pointing at itself. Not an error; expected on any library that
    #: does not own everything TMDB recommends.
    edges_skipped_not_in_library: int = 0


def wipe_edge_type(conn: sqlite3.Connection, edge_type: str) -> tuple[int, int]:
    """Delete every `edges` row and cursor row for one TMDB edge type, leaving
    every other edge type and every other enrichment namespace untouched.

    Returns `(edges_removed, cursor_rows_removed)`.
    """
    with conn:
        edges_removed = conn.execute("DELETE FROM edges WHERE edge_type = ?", (edge_type,)).rowcount
        cursor_removed = conn.execute(
            "DELETE FROM enrichment WHERE namespace = ? AND key = ?",
            (_CURSOR_NAMESPACE, _CURSOR_KEYS[edge_type]),
        ).rowcount
    return edges_removed, cursor_removed


def _prefetch(conn: sqlite3.Connection) -> tuple[dict[str, str], list[sqlite3.Row]]:
    """The two read-only queries every edge-type sweep needs, run once and
    shared across both — neither depends on `edge_type`, so re-running them
    per sweep would scan `external_ids` and `items` twice for no reason.

    Returns `(tmdb_to_item, candidates)`:

    - `tmdb_to_item`: the reverse index every fetched TMDB id is resolved
      through — tmdb id -> local item_id, prefetched into a dict rather than
      one SELECT per recommendation, the same shape `enrich_tmdb_keywords`
      uses. A TMDB id absent from here is a title nobody walked, and its
      edge is dropped, not invented.
    - `candidates`: every walked movie/show, with its local `tmdb` id if it
      has one. A correlated subquery, not a JOIN, so a title is visited
      exactly once even in the (schema-legal) case of more than one `tmdb`
      row landing on the same item_id — same query shape
      `enrich_tmdb_keywords` uses for its own candidates.
    """
    tmdb_to_item: dict[str, str] = {
        row["value"]: row["item_id"]
        for row in conn.execute("SELECT item_id, value FROM external_ids WHERE ns = 'tmdb'")
    }
    candidates = conn.execute(
        """
        SELECT
            i.item_id AS item_id,
            i.type AS type,
            (SELECT e.value FROM external_ids e
             WHERE e.item_id = i.item_id AND e.ns = 'tmdb' LIMIT 1) AS tmdb_id
        FROM items i
        WHERE i.type IN ('movie', 'show')
        """
    ).fetchall()
    return tmdb_to_item, candidates


def _sweep(
    conn: sqlite3.Connection,
    fetch: _Fetch,
    *,
    edge_type: str,
    stale_days: int,
    tmdb_to_item: dict[str, str],
    candidates: list[sqlite3.Row],
) -> EdgeStats:
    """Refresh one edge type across every walked movie/show carrying a local
    `tmdb` external id.

    Each title's `(from_id, edge_type)` set is deleted and rewritten inside
    one transaction — a run interrupted partway through never leaves a
    half-replaced set, and a title already handled by an earlier, successful
    transaction keeps what it wrote even if a later title in the same sweep
    fails.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=stale_days)
    now_iso = now.isoformat(timespec="seconds")
    stats = EdgeStats()
    cursor_key = _CURSOR_KEYS[edge_type]

    cached_fetched_at: dict[str, str] = {
        row["item_id"]: row["fetched_at"]
        for row in conn.execute(
            "SELECT item_id, fetched_at FROM enrichment "
            "WHERE namespace = ? AND key = ? AND value = ?",
            (_CURSOR_NAMESPACE, cursor_key, _SENTINEL_VALUE),
        )
    }

    consecutive_failures = 0

    for row in candidates:
        stats.titles_seen += 1
        item_id = row["item_id"]
        media_type = media_type_for(row["type"])
        tmdb_id = row["tmdb_id"]
        if media_type is None or tmdb_id is None:
            stats.titles_skipped_no_tmdb_id += 1
            continue

        fetched_at = cached_fetched_at.get(item_id)
        if fetched_at is not None and not is_stale(fetched_at, cutoff):
            stats.titles_cached += 1
            continue

        try:
            related_ids = fetch(tmdb_id, media_type)
        except TMDbError as err:
            stats.titles_failed += 1
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise TMDbError(
                    f"aborting after {MAX_CONSECUTIVE_FAILURES} consecutive failures: "
                    f"{stats.titles_seen} title(s) processed, {stats.titles_failed} failed; "
                    f"tripping error: {err}"
                ) from err
            continue

        consecutive_failures = 0

        # Rank is array position, 1-indexed — TMDB's own order, stored
        # verbatim rather than recomputed. A target outside the library, or
        # a title recommending itself, is dropped and counted, never
        # inserted under a placeholder id.
        #
        # `seen` guards a real case, not a hypothetical one: `tmdb_to_item`
        # can hold two different TMDB ids landing on the same item_id — the
        # same "more than one `tmdb` row per item_id" the `candidates` query
        # above is already commented as schema-legal for. If TMDB's related
        # list contained both ids, an unguarded loop would try to insert the
        # same `(item_id, to_item_id, edge_type)` twice at two different
        # ranks and crash the whole sweep on the table's own primary key
        # (`schema.py`'s `edges`) instead of just dropping the weaker rank —
        # `enrich_tmdb.py` faces the identical risk for keyword values and
        # guards it with `dict.fromkeys`.
        edges: list[tuple[str, int]] = []
        seen: set[str] = set()
        for position, related_tmdb_id in enumerate(related_ids, start=1):
            to_item_id = tmdb_to_item.get(related_tmdb_id)
            if to_item_id is None or to_item_id == item_id:
                stats.edges_skipped_not_in_library += 1
                continue
            if to_item_id in seen:
                continue
            seen.add(to_item_id)
            edges.append((to_item_id, position))

        with conn:
            conn.execute(
                "DELETE FROM edges WHERE from_id = ? AND edge_type = ?",
                (item_id, edge_type),
            )
            for to_item_id, rank in edges:
                conn.execute(
                    "INSERT INTO edges (from_id, to_id, edge_type, rank, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (item_id, to_item_id, edge_type, rank, now_iso),
                )
            conn.execute(
                "DELETE FROM enrichment WHERE item_id = ? AND namespace = ? AND key = ?",
                (item_id, _CURSOR_NAMESPACE, cursor_key),
            )
            conn.execute(
                "INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (item_id, _CURSOR_NAMESPACE, cursor_key, _SENTINEL_VALUE, now_iso),
            )
        stats.titles_fetched += 1
        stats.edges_written += len(edges)

    return stats


def refresh_tmdb_edges(
    conn: sqlite3.Connection,
    source: TMDbSource,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> dict[str, EdgeStats]:
    """Refresh both TMDB edge types, keyed by `edge_type` in the result.

    Each edge type is swept independently — a failure in one does not touch
    the other's cursor or rows — but the two read-only prefetch queries
    neither sweep's `edge_type` affects are shared between them rather than
    run twice.
    """
    tmdb_to_item, candidates = _prefetch(conn)
    return {
        RECOMMENDATIONS_EDGE_TYPE: _sweep(
            conn,
            source.recommendations,
            edge_type=RECOMMENDATIONS_EDGE_TYPE,
            stale_days=stale_days,
            tmdb_to_item=tmdb_to_item,
            candidates=candidates,
        ),
        SIMILAR_EDGE_TYPE: _sweep(
            conn,
            source.similar,
            edge_type=SIMILAR_EDGE_TYPE,
            stale_days=stale_days,
            tmdb_to_item=tmdb_to_item,
            candidates=candidates,
        ),
    }
