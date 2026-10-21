"""Fetching each movie's and show's original language from TMDB, cached.

TMDB's details endpoint names the language a title was made in as an ISO 639-1
code (`en`, `ja`). It becomes one row per title in `enrichment`, namespace
`tmdb`, source `tmdb`, key `original_language`, value the code exactly as TMDB
sent it. The store never interprets it; the "English original, no English
subtitle" recipe in Plex DB-EX's Query tab reads it beside `media_stream`.

An episode carries no row of its own: its language is its show's, reached
through `items.show_item_id`.

Caching follows `enrich_tmdb.py`: a title inside its staleness threshold is not
asked of `TMDbSource` at all, and one `enrichment_cursor` row per title
(namespace `tmdb`, source `tmdb`, key `fetched`) makes a title TMDB gave no
language for just as cacheable as one it did (ADR-0013). A fetch replaces the
title's `original_language` rows and moves its cursor in one transaction.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .cursors import delete_cursors, load_fetched, write_fetched
from .errors import TMDbError
from .staleness import DEFAULT_STALE_DAYS, is_stale
from .tmdb_client import TMDbSource
from .tmdb_common import MAX_CONSECUTIVE_FAILURES, media_type_for

#: The namespace this writer fills, in `enrichment` and `enrichment_cursor`.
NAMESPACE = "tmdb"
#: This writer's source name within it.
SOURCE = "tmdb"
KEY = "original_language"


@dataclass
class LanguageStats:
    """What one run touched — the summary `plexdb enrich-tmdb-language` prints."""

    titles_seen: int = 0
    titles_fetched: int = 0
    titles_cached: int = 0
    #: Movies and shows with no `tmdb` row in `external_ids` — skipped, not an error.
    titles_skipped_no_tmdb_id: int = 0
    titles_failed: int = 0
    #: Fetched titles TMDB named no language for: cached, but no row written.
    titles_without_language: int = 0
    languages_written: int = 0


def wipe(conn: sqlite3.Connection) -> int:
    """Delete this writer's `original_language` rows and its fetch cursors, and
    nothing else under `tmdb`. Returns the number of rows removed."""
    with conn:
        removed = conn.execute(
            "DELETE FROM enrichment WHERE namespace = ? AND source = ? AND key = ?",
            (NAMESPACE, SOURCE, KEY),
        ).rowcount
        removed += delete_cursors(conn, NAMESPACE, SOURCE)
        return removed


def enrich_tmdb_language(
    conn: sqlite3.Connection,
    source: TMDbSource,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> LanguageStats:
    """Record the original language of every walked movie and show carrying a
    `tmdb` external id. A title inside its staleness threshold is never asked
    of `source`. A `TMDbError` for one title is counted and the run moves on;
    `MAX_CONSECUTIVE_FAILURES` in a row abort it."""
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=stale_days)
    now_iso = now.isoformat(timespec="seconds")
    stats = LanguageStats()
    cached_fetched_at = load_fetched(conn, NAMESPACE, SOURCE)

    candidates = conn.execute(
        """
        SELECT
            i.item_id AS item_id,
            i.type AS type,
            (SELECT e.value FROM external_ids e
             WHERE e.item_id = i.item_id AND e.ns = 'tmdb' LIMIT 1) AS tmdb_id
        FROM items i
        WHERE i.type IN ('movie', 'show')
        ORDER BY i.item_id
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
            language = source.original_language(tmdb_id, media_type)
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
                "DELETE FROM enrichment "
                "WHERE item_id = ? AND namespace = ? AND source = ? AND key = ?",
                (item_id, NAMESPACE, SOURCE, KEY),
            )
            if language:
                conn.execute(
                    "INSERT INTO enrichment "
                    "(item_id, namespace, source, key, value, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (item_id, NAMESPACE, SOURCE, KEY, language, now_iso),
                )
            write_fetched(conn, NAMESPACE, SOURCE, item_id, now_iso)
        stats.titles_fetched += 1
        if language:
            stats.languages_written += 1
        else:
            stats.titles_without_language += 1

    return stats
