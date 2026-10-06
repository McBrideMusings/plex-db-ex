"""Scraping each movie's Letterboxd themes into `keywords`, cached per title.

Every theme and mini-theme label on a film's page becomes a keyword in the
shared `keywords` namespace under `source = 'letterboxd'` (ADR-0016), through
the same normalize-and-stem step TMDB's keywords take, with its raw spelling in
`keyword_forms`. Letterboxd ranks nothing and states no role, so `rank` stays
NULL and no `keyword_roles` row is written. Movies only: the lookup goes
through a TMDB *movie* id (`letterboxd_client.py`).

Each title's rows under this source are a snapshot: a re-fetch deletes and
rewrites them. One `enrichment_cursor` row per title (namespace `keywords`,
source `letterboxd`, key `fetched`) is written for a film page that parsed —
even with no themes — and for a movie Letterboxd does not list, so neither is
asked again until stale (ADR-0013).

There is no API contract, so two things are kept apart from a request failing:

- A film page without the film marker is a **parse failure**. It writes no
  keyword and no `fetched` cursor, so the next sweep asks again. When parse
  failures pass `MAX_PARSE_FAILURE_SHARE` of the film pages fetched,
  `parse_failed` says so and the command exits non-zero after printing the
  count, so a layout change shows in the sweep summary instead of as empty
  theme sets.
- A failed request writes no keyword and no `fetched` cursor either; three in a
  row abort the run.

Both record only an `attempted` cursor (same namespace and source), which is
scheduling, not a fact: it never makes a title fresh, and a later successful
fetch deletes it.

A run fetches at most `limit` titles (`DEFAULT_MAX_TITLES` unless
`LETTERBOXD_MAX_TITLES` or `--limit` says otherwise). Due titles never
attempted come first, in `item_id` order, then attempted ones, oldest attempt
first. A fetched title's cursor takes it out of the next run's list, and an
attempted one moves to the back of it, so the first pass over the library
spreads across several sweeps, each continuing where the last stopped, and a
title that keeps failing never holds the cap.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .cursors import load_cursors, mark_attempted, write_fetched
from .errors import LetterboxdError
from .keywords import NAMESPACE, upsert_keyword_form
from .letterboxd_client import LetterboxdSource, Lookup, Outcome, is_tmdb_id
from .staleness import DEFAULT_STALE_DAYS, is_stale

#: This writer's source name, in `keywords` and `enrichment_cursor`.
SOURCE = "letterboxd"
_KEYWORD_KEY = "keyword"

#: Titles one run fetches at most. Two requests and about 2.5 s per title, so
#: one run takes about 40 minutes and the ~11,700 movies with a TMDB id are
#: covered in about 12 sweeps.
DEFAULT_MAX_TITLES = 1000

#: Share of film pages fetched that may lack the film marker before the run
#: exits non-zero.
MAX_PARSE_FAILURE_SHARE = 0.10

#: Failed titles in a row that abort the run — the site is down, or
#: challenging this client, and asking for every remaining title only adds to it.
MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class LetterboxdStats:
    """What one run touched — the summary `plexdb enrich-letterboxd` prints."""

    titles_seen: int = 0
    titles_cached: int = 0
    #: Movies with no usable `tmdb` row in `external_ids` — skipped, not an error.
    titles_skipped_no_tmdb_id: int = 0
    #: Due titles past the per-run cap, left for the next run.
    titles_capped: int = 0
    #: Titles that got a cursor this run: listed and parsed, or not listed.
    titles_fetched: int = 0
    titles_not_listed: int = 0
    #: Parsed film pages carrying at least one theme.
    titles_matched: int = 0
    #: Film pages without the marker. Nothing is written for them.
    parse_failures: int = 0
    #: Titles whose request failed. Nothing is written for them.
    titles_failed: int = 0
    keywords_written: int = 0

    @property
    def pages_fetched(self) -> int:
        """Film pages that loaded, parsed or not — the base of the failure share."""
        return self.titles_fetched - self.titles_not_listed + self.parse_failures

    @property
    def parse_failed(self) -> bool:
        return (
            self.pages_fetched > 0
            and self.parse_failures / self.pages_fetched > MAX_PARSE_FAILURE_SHARE
        )


def wipe(conn: sqlite3.Connection) -> int:
    """Delete this source's keywords and cursors. Every other source and
    namespace stays. Returns the number of rows removed."""
    with conn:
        removed = conn.execute(
            "DELETE FROM enrichment WHERE namespace = ? AND source = ?", (NAMESPACE, SOURCE)
        ).rowcount
        removed += conn.execute(
            "DELETE FROM enrichment_cursor WHERE namespace = ? AND source = ?",
            (NAMESPACE, SOURCE),
        ).rowcount
        return removed


def enrich_letterboxd(
    conn: sqlite3.Connection,
    source: LetterboxdSource,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    limit: int = DEFAULT_MAX_TITLES,
) -> LetterboxdStats:
    """Look up every walked movie with a `tmdb` external id whose cursor is
    missing or stale, at most `limit` of them: never-attempted titles first in
    `item_id` order, then attempted ones, oldest attempt first."""
    cutoff = datetime.now(UTC) - timedelta(days=stale_days)
    stats = LetterboxdStats()

    cursors = load_cursors(conn, NAMESPACE, SOURCE)
    # A join, not a correlated subquery: the planner answers `e.ns = 'tmdb'`
    # inside a subquery from the (ns, value, kind) key and scans every tmdb row
    # once per movie — minutes on the real store, against 0.06 s for this.
    candidates = conn.execute(
        """
        SELECT i.item_id AS item_id, MIN(e.value) AS tmdb_id
        FROM items i
        LEFT JOIN external_ids e ON e.item_id = i.item_id AND e.ns = 'tmdb'
        WHERE i.type = 'movie'
        GROUP BY i.item_id
        ORDER BY i.item_id
        """
    ).fetchall()

    due: list[tuple[str, str]] = []
    for row in candidates:
        stats.titles_seen += 1
        if row["tmdb_id"] is None or not is_tmdb_id(row["tmdb_id"]):
            stats.titles_skipped_no_tmdb_id += 1
            continue
        fetched_at = cursors.fetched.get(row["item_id"])
        if fetched_at is not None and not is_stale(fetched_at, cutoff):
            stats.titles_cached += 1
            continue
        due.append((row["item_id"], row["tmdb_id"]))
    # Stable, so titles never attempted keep their `item_id` order.
    due.sort(key=lambda title: cursors.order_key(title[0]))
    stats.titles_capped = max(0, len(due) - limit)

    consecutive_failures = 0
    for item_id, tmdb_id in due[:limit]:
        try:
            found = source.lookup(tmdb_id)
        except LetterboxdError as err:
            stats.titles_failed += 1
            consecutive_failures += 1
            mark_attempted(conn, NAMESPACE, SOURCE, [item_id])
            print(f"letterboxd: tmdb {tmdb_id} failed: {err}", flush=True)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise LetterboxdError(
                    f"aborting after {MAX_CONSECUTIVE_FAILURES} consecutive failed titles: "
                    f"{stats.titles_fetched} title(s) fetched, {stats.titles_failed} failed; "
                    f"tripping error: {err}"
                ) from err
            continue
        consecutive_failures = 0
        if found.outcome is Outcome.UNPARSED:
            stats.parse_failures += 1
            mark_attempted(conn, NAMESPACE, SOURCE, [item_id])
            continue
        _write_title(conn, item_id, found, datetime.now(UTC), stats)

    return stats


def _write_title(
    conn: sqlite3.Connection,
    item_id: str,
    found: Lookup,
    now: datetime,
    stats: LetterboxdStats,
) -> None:
    """Replace one title's rows and its cursor in one transaction, so an
    interrupted run never leaves a cursor vouching for rows already deleted."""
    now_iso = now.isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "DELETE FROM enrichment WHERE item_id = ? AND namespace = ? AND source = ?",
            (item_id, NAMESPACE, SOURCE),
        )
        write_fetched(conn, NAMESPACE, SOURCE, item_id, now_iso)
        keywords: dict[str, None] = {}
        for label in found.themes:
            keyword = upsert_keyword_form(conn, label.strip())
            if keyword:
                keywords[keyword] = None
        for keyword in keywords:
            conn.execute(
                "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, NAMESPACE, SOURCE, _KEYWORD_KEY, keyword, now_iso),
            )
    stats.titles_fetched += 1
    stats.keywords_written += len(keywords)
    if found.outcome is Outcome.NOT_LISTED:
        stats.titles_not_listed += 1
    elif keywords:
        stats.titles_matched += 1
