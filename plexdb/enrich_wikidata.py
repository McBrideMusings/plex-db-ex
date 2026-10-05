"""Fetching what Wikidata states about each title into `keywords`, `awards` and
`keyword_roles`, cached per title.

Four properties become keywords in the shared `keywords` namespace under
`source = 'wikidata'` (ADR-0016): narrative location (P840), set in period
(P2408), main subject (P921) and genre (P136). Each label goes through the same
normalize-and-stem step TMDB's keywords do, with its raw spelling recorded in
`keyword_forms`, so Wikidata's `heist film` and TMDB's `heist film` are one
stored keyword.

Two of those properties also say what the keyword *is*: a narrative location is
a place, and a period is a time. Those become `keyword_roles` rows with role
`region` and `era`, `source = 'wikidata'`, and score, model and error NULL —
stated by the source, not judged (ADR-0019). Main subject and genre carry no
role. A role row is per keyword, not per title, so a title's refresh leaves it
alone; `--rewipe` is what clears them.

Award received (P166) is not a keyword. Its labels go verbatim into their own
`awards` namespace, key `award` — stemming "Academy Award for Best Sound" would
make it unreadable and match nothing a keyword source says.

Like TMDB, each title's rows under this source are a snapshot: a re-fetch
deletes and rewrites them. One `enrichment_cursor` row per title (namespace
`keywords`, source `wikidata`, key `fetched`) covers both namespaces, and is
written even when Wikidata has nothing, so a title with no Wikidata item is not
asked again until it is stale (ADR-0013).

Titles are asked in batches, one SPARQL query each (`wikidata_client.py`); a
batch that fails writes nothing for any of its titles, so a re-run retries them.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .errors import WikidataError
from .keywords import NAMESPACE, state_role, upsert_keyword_form
from .staleness import DEFAULT_STALE_DAYS, is_stale
from .wikidata_client import (
    AWARD_RECEIVED,
    GENRE,
    MAIN_SUBJECT,
    NARRATIVE_LOCATION,
    SET_IN_PERIOD,
    WikidataSource,
    is_imdb_id,
)

#: This writer's source name, in `keywords`, `awards`, `keyword_roles` and
#: `enrichment_cursor`.
SOURCE = "wikidata"
AWARDS_NAMESPACE = "awards"
_KEYWORD_KEY = "keyword"
_AWARD_KEY = "award"
_CURSOR_KEY = "fetched"

#: Properties whose labels become keywords, and the role each one states, if any.
KEYWORD_PROPERTIES: dict[str, str | None] = {
    NARRATIVE_LOCATION: "region",
    SET_IN_PERIOD: "era",
    MAIN_SUBJECT: None,
    GENRE: None,
}

#: IMDb ids per SPARQL query. 400 answered in 4.9 s on 2026-10-03; half that
#: leaves a slow day an order of magnitude inside the 60 s deadline.
BATCH_SIZE = 200

#: Failed batches in a row that abort the sweep — the service is down, or
#: refusing this client, and asking again for every remaining batch only adds
#: to whatever made it refuse.
MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class WikidataStats:
    """What one sweep touched — the summary `plexdb enrich-wikidata` prints."""

    titles_seen: int = 0
    titles_fetched: int = 0
    titles_cached: int = 0
    #: Movies/shows with no usable `imdb` row in `external_ids` — skipped, not
    #: an error. A value that is not `tt` plus digits counts here too, so one bad
    #: row cannot fail the 199 titles batched with it.
    titles_skipped_no_imdb_id: int = 0
    #: Fetched titles Wikidata stated at least one of the five properties for.
    titles_matched: int = 0
    #: Titles in a batch whose query failed. Nothing is written for them.
    titles_failed: int = 0
    queries_sent: int = 0
    keywords_written: int = 0
    #: Distinct (keyword, role) rows this run stated or restated.
    roles_stated: int = 0
    awards_written: int = 0


def wipe(conn: sqlite3.Connection) -> int:
    """Delete everything this source owns: its keywords, its awards, its role
    rows and its cursors. Another source's keywords and roles, and every other
    namespace, stay. Returns the number of rows removed."""
    with conn:
        removed = conn.execute(
            "DELETE FROM enrichment WHERE namespace IN (?, ?) AND source = ?",
            (NAMESPACE, AWARDS_NAMESPACE, SOURCE),
        ).rowcount
        removed += conn.execute("DELETE FROM keyword_roles WHERE source = ?", (SOURCE,)).rowcount
        removed += conn.execute(
            "DELETE FROM enrichment_cursor WHERE namespace = ? AND source = ?",
            (NAMESPACE, SOURCE),
        ).rowcount
        return removed


def enrich_wikidata(
    conn: sqlite3.Connection,
    source: WikidataSource,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    batch_size: int = BATCH_SIZE,
) -> WikidataStats:
    """Ask Wikidata about every walked movie/show with an `imdb` external id
    whose cursor is missing or stale, `batch_size` titles per query."""
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=stale_days)
    stats = WikidataStats()

    cached_fetched_at: dict[str, str] = {
        row["item_id"]: row["fetched_at"]
        for row in conn.execute(
            "SELECT item_id, fetched_at FROM enrichment_cursor "
            "WHERE namespace = ? AND source = ? AND key = ?",
            (NAMESPACE, SOURCE, _CURSOR_KEY),
        )
    }
    candidates = conn.execute(
        """
        SELECT i.item_id AS item_id, MIN(e.value) AS imdb_id
        FROM items i
        LEFT JOIN external_ids e ON e.item_id = i.item_id AND e.ns = 'imdb'
        WHERE i.type IN ('movie', 'show')
        GROUP BY i.item_id
        ORDER BY i.item_id
        """
    ).fetchall()

    due: list[tuple[str, str]] = []
    for row in candidates:
        stats.titles_seen += 1
        if row["imdb_id"] is None or not is_imdb_id(row["imdb_id"]):
            stats.titles_skipped_no_imdb_id += 1
            continue
        fetched_at = cached_fetched_at.get(row["item_id"])
        if fetched_at is not None and not is_stale(fetched_at, cutoff):
            stats.titles_cached += 1
            continue
        due.append((row["item_id"], row["imdb_id"]))

    stated: set[tuple[str, str]] = set()
    consecutive_failures = 0
    for start in range(0, len(due), batch_size):
        batch = due[start : start + batch_size]
        imdb_ids = list(dict.fromkeys(imdb_id for _, imdb_id in batch))
        stats.queries_sent += 1
        try:
            statements = source.statements(imdb_ids)
        except WikidataError as err:
            stats.titles_failed += len(batch)
            consecutive_failures += 1
            print(f"wikidata: batch of {len(batch)} failed: {err}", flush=True)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise WikidataError(
                    f"aborting after {MAX_CONSECUTIVE_FAILURES} consecutive failed queries: "
                    f"{stats.titles_fetched} title(s) fetched, {stats.titles_failed} failed; "
                    f"tripping error: {err}"
                ) from err
            continue
        consecutive_failures = 0

        by_imdb: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for imdb_id, prop, label in statements:
            by_imdb[imdb_id].append((prop, label))
        _write_batch(conn, batch, by_imdb, datetime.now(UTC), stats, stated)

    stats.roles_stated = len(stated)
    return stats


def _write_batch(
    conn: sqlite3.Connection,
    batch: list[tuple[str, str]],
    by_imdb: dict[str, list[tuple[str, str]]],
    now: datetime,
    stats: WikidataStats,
    stated: set[tuple[str, str]],
) -> None:
    """Replace every title's rows in one transaction, cursors included, so an
    interrupted sweep never leaves a cursor vouching for rows already deleted."""
    now_iso = now.isoformat(timespec="seconds")
    with conn:
        for item_id, imdb_id in batch:
            conn.execute(
                "DELETE FROM enrichment WHERE item_id = ? AND namespace IN (?, ?) AND source = ?",
                (item_id, NAMESPACE, AWARDS_NAMESPACE, SOURCE),
            )
            conn.execute(
                "INSERT INTO enrichment_cursor (item_id, namespace, source, key, fetched_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(item_id, namespace, source, key) DO UPDATE SET "
                "fetched_at = excluded.fetched_at",
                (item_id, NAMESPACE, SOURCE, _CURSOR_KEY, now_iso),
            )
            keywords: dict[str, None] = {}
            awards: dict[str, None] = {}
            for prop, label in by_imdb.get(imdb_id, []):
                label = label.strip()
                if not label:
                    continue
                if prop == AWARD_RECEIVED:
                    awards[label] = None
                    continue
                if prop not in KEYWORD_PROPERTIES:
                    continue
                keyword = upsert_keyword_form(conn, label)
                if not keyword:
                    continue
                keywords[keyword] = None
                role = KEYWORD_PROPERTIES[prop]
                if role is not None:
                    state_role(conn, keyword, role, SOURCE, now_iso)
                    stated.add((keyword, role))
            for keyword in keywords:
                conn.execute(
                    "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (item_id, NAMESPACE, SOURCE, _KEYWORD_KEY, keyword, now_iso),
                )
            for award in awards:
                conn.execute(
                    "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (item_id, AWARDS_NAMESPACE, SOURCE, _AWARD_KEY, award, now_iso),
                )
            stats.titles_fetched += 1
            stats.keywords_written += len(keywords)
            stats.awards_written += len(awards)
            if keywords or awards:
                stats.titles_matched += 1
