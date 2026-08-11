"""Fetching TMDB keywords into the `tmdb_keywords` enrichment namespace, cached.

Enrichment is namespaced and opaque (`docs/schema.md`): this module owns exactly
one namespace, `tmdb_keywords`, and never touches another writer's rows. A
title's row set here is a snapshot, not an appended log — on a fresh fetch the
whole set for that title is replaced, the same wipe-and-rewrite discipline the
edges design uses.

**Caching is the substance of this module, not a side effect.** A title inside
its staleness threshold is never asked of `TMDbSource` at all — the check
happens before the call, not after inspecting what came back. Without a
lookup for a title that turned out to carry zero keywords, that title would
be re-asked on every single sweep forever; `_SENTINEL_KEY` exists so a
zero-keyword result is just as cacheable as a twenty-keyword one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .errors import TMDbError
from .staleness import DEFAULT_STALE_DAYS, is_stale
from .tmdb_client import TMDbSource
from .tmdb_common import MAX_CONSECUTIVE_FAILURES, media_type_for

#: This writer's namespace. No other module may write rows under it.
NAMESPACE = "tmdb_keywords"
_KEYWORD_KEY = "keyword"
#: One row per enriched title regardless of how many keywords it carries, so
#: a title with zero keywords still has a `fetched_at` to check staleness
#: against.
_SENTINEL_KEY = "_fetched"
_SENTINEL_VALUE = "1"


@dataclass
class EnrichStats:
    """What one sweep touched — the summary `plexdb enrich-tmdb-keywords` prints."""

    titles_seen: int = 0
    titles_fetched: int = 0
    titles_cached: int = 0
    #: Movies/shows with no `tmdb` row in `external_ids` — skipped, not an
    #: error. TVDB-only shows and anime are expected here, not exceptional.
    titles_skipped_no_tmdb_id: int = 0
    #: A single title's fetch raised `TMDbError`. Nothing is written for it,
    #: so a re-run retries it; `MAX_CONSECUTIVE_FAILURES` in a row abort the
    #: sweep.
    titles_failed: int = 0
    keywords_written: int = 0


def wipe_namespace(conn: sqlite3.Connection) -> int:
    """Delete every `tmdb_keywords` enrichment row, leaving every other
    namespace's rows untouched. Returns the number of rows removed.

    Scoped to this module's own namespace, matching the module docstring's
    claim to own exactly one. A version taking any namespace belongs in
    `store.py` once a second enrichment writer needs one — not before.
    """
    with conn:
        cursor = conn.execute("DELETE FROM enrichment WHERE namespace = ?", (NAMESPACE,))
        return cursor.rowcount


def enrich_tmdb_keywords(
    conn: sqlite3.Connection,
    source: TMDbSource,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> EnrichStats:
    """Fetch TMDB keywords for every walked movie/show carrying a `tmdb`
    external id, writing them under the `tmdb_keywords` namespace.

    A title inside its staleness threshold is never asked of `source` at
    all. Each title's row set is committed on its own, so a sweep
    interrupted partway through a large library keeps everything it already
    fetched rather than losing the whole pass and re-asking TMDB for it.

    A single title's `TMDbError` is counted in `titles_failed` and the sweep
    moves on — nothing is written for that title, so a re-run retries it.
    `MAX_CONSECUTIVE_FAILURES` failures in a row raise instead, aborting the
    sweep; any success resets the run.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=stale_days)
    now_iso = now.isoformat(timespec="seconds")
    stats = EnrichStats()

    # Every title's cached fetch time, read once up front rather than one
    # SELECT per title inside the loop below — the same prefetch-into-a-dict
    # shape `walk_all` uses for `existing_by_rating_key`. Each item_id in
    # `items` is visited once per sweep, so a dict built before the loop
    # starts stays correct for the whole pass.
    cached_fetched_at: dict[str, str] = {
        row["item_id"]: row["fetched_at"]
        for row in conn.execute(
            "SELECT item_id, fetched_at FROM enrichment "
            "WHERE namespace = ? AND key = ? AND value = ?",
            (NAMESPACE, _SENTINEL_KEY, _SENTINEL_VALUE),
        )
    }

    # A correlated subquery, not a JOIN, so a title is visited exactly once
    # even in the (schema-legal) case of more than one `tmdb` row landing on
    # the same item_id.
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
            keywords = source.keywords(tmdb_id, media_type)
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

        with conn:
            conn.execute(
                "DELETE FROM enrichment WHERE item_id = ? AND namespace = ?",
                (item_id, NAMESPACE),
            )
            conn.execute(
                "INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (item_id, NAMESPACE, _SENTINEL_KEY, _SENTINEL_VALUE, now_iso),
            )
            # `dict.fromkeys` dedupes while keeping first-seen order, in case
            # a source ever repeats a name — the row's primary key includes
            # `value`, so an unguarded duplicate would raise mid-insert.
            unique_keywords = list(dict.fromkeys(keywords))
            for keyword in unique_keywords:
                conn.execute(
                    "INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (item_id, NAMESPACE, _KEYWORD_KEY, keyword, now_iso),
                )
        stats.titles_fetched += 1
        stats.keywords_written += len(unique_keywords)

    return stats
