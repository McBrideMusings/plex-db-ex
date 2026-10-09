"""Fetching TMDB keywords into the `keywords` enrichment namespace, cached.

Enrichment is namespaced and opaque (`docs/schema.md`): this module owns exactly
one `source` within the shared `keywords` namespace, `tmdb` (ADR-0016), and
never touches another source's rows — a refresh here deletes and rewrites only
`WHERE namespace = 'keywords' AND source = 'tmdb'`, so a keyword another source
also lists survives this module wiping its own. A title's row set under this
source is a snapshot, not an appended log — on a fresh fetch the whole set for
that title-and-source is replaced, the same wipe-and-rewrite discipline the
edges design uses.

Every keyword is normalized and stemmed before it is stored
(`keywords.normalize_keyword`) and every raw spelling TMDB returned is recorded
in `keyword_forms`, so `heists` and `heist` land on one row and a reader can
still show the spelling TMDB actually used.

**Caching is the substance of this module, not a side effect.** A title inside
its staleness threshold is never asked of `TMDbSource` at all — the check
happens before the call, not after inspecting what came back. A title that
turned out to carry zero keywords leaves no `enrichment` row to read a
timestamp off, so without a marker it would be re-asked on every sweep
forever; the per-title cursor row exists so a zero-keyword result is just as
cacheable as a twenty-keyword one.

That cursor lives in `enrichment_cursor`, not in `enrichment` (ADR-0013).
`enrichment` holds facts about titles; a reader scanning it needs no
convention to tell a keyword from this module's own bookkeeping, because the
bookkeeping is not there.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .cursors import delete_cursors, load_fetched, write_fetched
from .errors import TMDbError

# Re-exported (redundant `as` alias) so tests can check that every sweep binds
# the exact same function/constant object rather than a drifted copy — see
# test_staleness.py and test_tmdb_common.py.
from .keywords import NAMESPACE as NAMESPACE
from .keywords import RawKeyword, write_title_keywords
from .staleness import DEFAULT_STALE_DAYS as DEFAULT_STALE_DAYS
from .staleness import is_stale as is_stale
from .tmdb_client import TMDbSource
from .tmdb_common import MAX_CONSECUTIVE_FAILURES as MAX_CONSECUTIVE_FAILURES
from .tmdb_common import media_type_for as media_type_for

#: The source-agnostic namespace every keyword source shares (ADR-0016), and
#: this writer's own source name within it. No other module may write rows
#: under `SOURCE`; another keyword source writes its own rows under this same
#: `NAMESPACE`, tagged with its own `source`.
SOURCE = "tmdb"


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
    """Delete every `tmdb`-sourced `keywords` row **and** this module's fetch
    cursors, leaving every other source's keywords — and every other namespace
    — untouched. Returns the number of rows removed.

    Scoped to `source = SOURCE` on both tables (ADR-0016): a keyword another
    source also lists on a title must survive this module wiping its own, and
    so must that other source's own fetch cursor — a bare `namespace = ?`
    delete on `enrichment_cursor` would erase every source's cursor the
    moment a second keyword source shares this namespace, not just this
    module's.

    Both tables, because they hold one module's state split across two places
    for a reader's benefit (ADR-0013), not two independent things. Wiping the
    keywords and keeping the cursors would leave every title looking fetched
    and empty, so `--rewipe` would silently fetch nothing.
    """
    with conn:
        removed = conn.execute(
            "DELETE FROM enrichment WHERE namespace = ? AND source = ?", (NAMESPACE, SOURCE)
        ).rowcount
        conn.execute("DELETE FROM keyword_surfaces WHERE source = ?", (SOURCE,))
        removed += delete_cursors(conn, NAMESPACE, SOURCE)
        return removed


def enrich_tmdb_keywords(
    conn: sqlite3.Connection,
    source: TMDbSource,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> EnrichStats:
    """Fetch TMDB keywords for every walked movie/show carrying a `tmdb`
    external id, writing them under the `keywords` namespace as `source='tmdb'`.

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
    cached_fetched_at = load_fetched(conn, NAMESPACE, SOURCE)

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
            # The cursor and the keywords it vouches for are replaced in one
            # transaction, together or not at all. Split across two, an
            # interrupted sweep could leave a cursor saying "fetched" over
            # keywords that had already been deleted.
            write_fetched(conn, NAMESPACE, SOURCE, item_id, now_iso)
            stored = write_title_keywords(
                conn, item_id, SOURCE, (RawKeyword(k) for k in keywords), now_iso
            )
        stats.titles_fetched += 1
        stats.keywords_written += len(set(stored.values()))

    return stats
