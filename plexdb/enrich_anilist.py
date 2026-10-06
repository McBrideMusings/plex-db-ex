"""Fetching AniList's tags for anime titles into `keywords` and `keyword_roles`,
with the rank AniList gives each tag, cached per title.

AniList has no TMDB, IMDb or TVDB id, so a title reaches it through Fribb's
mapping (`anilist_client.py`): the title's TMDB id (matched on movie vs. show),
else its IMDb id, else — for a show — its TVDB id, first hit wins, the same
precedence spirit as `item_id` itself. A walked title none of those map is not
anime for this source: it costs no request and gets no cursor.

**One title, several AniList entries.** AniList lists each season of a show
as its own entry, and a Plex show covers them all, so a show maps to every entry
its id names (127 of the 246 mapped shows in the store did, on 2026-10-03). Their
tags merge into the one title: a tag keeps its highest rank across the entries,
and is a spoiler if any entry flags it as one, so no season's spoiler reaches
key `keyword`.

Each tag goes through the same normalize-and-stem step TMDB's keywords do,
under `source = 'anilist'`, with AniList's 0–100 rank in `enrichment.rank`
verbatim. A spoiler tag (`isMediaSpoiler` or `isGeneralSpoiler`) is stored under
key `spoiler_keyword` instead of `keyword`, so a reader of key `keyword` never
sees it.

A tag's category states a role through `CATEGORY_ROLES` — `Theme-Fantasy` is a
`theme` — written to `keyword_roles` with score, model and error NULL
(ADR-0019). A role row is per keyword, not per title, so a title's refresh
leaves it alone; `--rewipe` is what clears them.

Each title's rows under this source are a snapshot: a re-fetch deletes and
rewrites them. One `enrichment_cursor` row per title (namespace `keywords`,
source `anilist`, key `fetched`) is written even when AniList had no tags, so the
title is not asked again until it is stale (ADR-0013). A title that drops out
of the mapping keeps the rows it has until `--rewipe`: a mapping that loses an
entry is more often an upstream slip than a title that stopped being anime.

Titles are asked in batches of up to `PAGE_SIZE` AniList ids; a batch that fails
writes nothing for any of its titles, so a re-run retries them.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .anilist_client import PAGE_SIZE, AniListSource, Tag
from .cursors import load_fetched, upsert_fetched
from .errors import AniListError
from .keywords import NAMESPACE, state_role, upsert_keyword_form
from .staleness import DEFAULT_STALE_DAYS, is_stale

#: This writer's source name, in `keywords`, `keyword_roles` and `enrichment_cursor`.
SOURCE = "anilist"
KEYWORD_KEY = "keyword"
SPOILER_KEY = "spoiler_keyword"

#: AniList tag category → the role it states. A category matches its own entry
#: or, failing that, its nearest `-`-separated parent: `Theme-Other-Organisations`
#: falls back to `Theme-Other`, then `Theme`. A category with no match states no
#: role. `docs/schema.md` documents this table.
CATEGORY_ROLES: dict[str, str] = {
    "Theme": "theme",
    "Setting-Time": "era",
    "Cast-Traits": "character_trait",
}

#: Failed batches in a row that abort the sweep — AniList is down, or refusing
#: this client, and asking again for every remaining batch only adds to it.
MAX_CONSECUTIVE_FAILURES = 3


def role_for(category: str | None) -> str | None:
    """The role an AniList tag category states, through `CATEGORY_ROLES`."""
    while category:
        if category in CATEGORY_ROLES:
            return CATEGORY_ROLES[category]
        category = category.rpartition("-")[0]
    return None


@dataclass
class AnimeIndex:
    """Fribb's mapping, turned into lookups from the ids a title carries."""

    #: `(item type, TMDB id)` → AniList ids. TMDB's movie and TV ids overlap,
    #: so the kind is part of the key.
    tmdb: dict[tuple[str, str], list[int]] = field(default_factory=lambda: defaultdict(list))
    imdb: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    #: TVDB series ids; a movie's TVDB id is a different id space and never looked up.
    tvdb: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    #: Mapping entries with an `anilist_id`.
    size: int = 0

    def lookup(
        self, kind: str, tmdb_ids: Iterable[str], imdb_ids: Iterable[str], tvdb_ids: Iterable[str]
    ) -> list[int]:
        """Every AniList id the first id namespace that hits maps this title to:
        TMDB, then IMDb, then TVDB for a show."""
        for hits in (
            [a for t in tmdb_ids for a in self.tmdb.get((kind, t), [])],
            [a for i in imdb_ids for a in self.imdb.get(i, [])],
            [a for t in tvdb_ids for a in self.tvdb.get(t, [])] if kind == "show" else [],
        ):
            if hits:
                return sorted(set(hits))
        return []


def _flatten(value: Any) -> list[str]:
    """Every id inside one Fribb field: a scalar, a list, or an object keyed
    by media type (`{"tv": 31911}`), nested in any of those shapes."""
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, int):
        return [str(value)]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [v for item in value for v in _flatten(item)]
    if isinstance(value, dict):
        return [v for item in value.values() for v in _flatten(item)]
    return []


def index_mapping(entries: Iterable[dict[str, Any]]) -> AnimeIndex:
    """Fribb's entries → an `AnimeIndex`. An entry with no integer `anilist_id`
    is skipped. A `themoviedb_id` object keyed `movie`/`tv` says the kind; a bare
    scalar or list takes it from the entry's `type` (`MOVIE`, or a show)."""
    index = AnimeIndex()
    for entry in entries:
        anilist_id = entry.get("anilist_id")
        if not isinstance(anilist_id, int) or isinstance(anilist_id, bool):
            continue
        index.size += 1
        tmdb = entry.get("themoviedb_id")
        if isinstance(tmdb, dict):
            for media_type, ids in tmdb.items():
                kind = {"movie": "movie", "tv": "show"}.get(media_type)
                if kind is not None:
                    for tmdb_id in _flatten(ids):
                        index.tmdb[(kind, tmdb_id)].append(anilist_id)
        else:
            kind = "movie" if entry.get("type") == "MOVIE" else "show"
            for tmdb_id in _flatten(tmdb):
                index.tmdb[(kind, tmdb_id)].append(anilist_id)
        for imdb_id in _flatten(entry.get("imdb_id")):
            index.imdb[imdb_id].append(anilist_id)
        for tvdb_id in _flatten(entry.get("tvdb_id")):
            index.tvdb[tvdb_id].append(anilist_id)
    return index


@dataclass
class AniListStats:
    """What one sweep touched — the summary `plexdb enrich-anilist` prints."""

    titles_seen: int = 0
    #: Movies/shows the mapping places on AniList.
    titles_anime: int = 0
    titles_fetched: int = 0
    titles_cached: int = 0
    #: Fetched titles AniList gave at least one tag for.
    titles_matched: int = 0
    #: Titles in a batch whose request failed. Nothing is written for them.
    titles_failed: int = 0
    batches_sent: int = 0
    anilist_ids_asked: int = 0
    keywords_written: int = 0
    spoiler_keywords_written: int = 0
    #: Distinct (keyword, role) rows this run stated or restated.
    roles_stated: int = 0


def wipe(conn: sqlite3.Connection) -> int:
    """Delete everything this source owns: its keywords and spoiler keywords,
    its role rows and its cursors. Another source's rows stay. Returns the number
    of rows removed."""
    with conn:
        removed = conn.execute(
            "DELETE FROM enrichment WHERE namespace = ? AND source = ?", (NAMESPACE, SOURCE)
        ).rowcount
        removed += conn.execute("DELETE FROM keyword_roles WHERE source = ?", (SOURCE,)).rowcount
        removed += conn.execute(
            "DELETE FROM enrichment_cursor WHERE namespace = ? AND source = ?",
            (NAMESPACE, SOURCE),
        ).rowcount
        return removed


def enrich_anilist(
    conn: sqlite3.Connection,
    source: AniListSource,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    batch_ids: int = PAGE_SIZE,
) -> AniListStats:
    """Ask AniList for the tags of every walked anime movie/show whose cursor is
    missing or stale, batching titles up to `batch_ids` AniList ids per request."""
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=stale_days)
    stats = AniListStats()

    cached_fetched_at = load_fetched(conn, NAMESPACE, SOURCE)
    kinds: dict[str, str] = {
        row["item_id"]: row["type"]
        for row in conn.execute("SELECT item_id, type FROM items WHERE type IN ('movie', 'show')")
    }
    ids: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for row in conn.execute(
        "SELECT e.item_id, e.ns, e.value FROM external_ids e "
        "JOIN items i ON i.item_id = e.item_id "
        "WHERE i.type IN ('movie', 'show') AND e.ns IN ('tmdb', 'imdb', 'tvdb') "
        "ORDER BY e.item_id, e.ns, e.value"
    ):
        ids[row["item_id"]][row["ns"]].append(row["value"])

    index = index_mapping(source.mapping())
    if not index.size:
        # Every title would read as "not anime" and be skipped: an upstream
        # breakage, not a library with no anime in it.
        raise AniListError("the anime mapping holds no entry with an anilist_id")

    due: list[tuple[str, list[int]]] = []
    for item_id, kind in kinds.items():
        stats.titles_seen += 1
        own = ids.get(item_id, {})
        anilist_ids = index.lookup(
            kind, own.get("tmdb", []), own.get("imdb", []), own.get("tvdb", [])
        )
        if not anilist_ids:
            continue
        stats.titles_anime += 1
        fetched_at = cached_fetched_at.get(item_id)
        if fetched_at is not None and not is_stale(fetched_at, cutoff):
            stats.titles_cached += 1
            continue
        due.append((item_id, anilist_ids))

    stated: set[tuple[str, str]] = set()
    consecutive_failures = 0
    for batch in _batches(due, batch_ids):
        asked = sorted({a for _, title_ids in batch for a in title_ids})
        stats.batches_sent += 1
        stats.anilist_ids_asked += len(asked)
        try:
            found = source.tags(asked)
        except AniListError as err:
            stats.titles_failed += len(batch)
            consecutive_failures += 1
            print(f"anilist: batch of {len(batch)} title(s) failed: {err}", flush=True)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise AniListError(
                    f"aborting after {MAX_CONSECUTIVE_FAILURES} consecutive failed batches: "
                    f"{stats.titles_fetched} title(s) fetched, {stats.titles_failed} failed; "
                    f"tripping error: {err}"
                ) from err
            continue
        consecutive_failures = 0
        _write_batch(conn, batch, found, datetime.now(UTC), stats, stated)

    stats.roles_stated = len(stated)
    return stats


def _batches(
    due: list[tuple[str, list[int]]], batch_ids: int
) -> Iterable[list[tuple[str, list[int]]]]:
    """Group titles so each batch asks for at most `batch_ids` AniList ids. A
    title mapping to more than that goes alone; the client pages through it."""
    batch: list[tuple[str, list[int]]] = []
    size = 0
    for item_id, anilist_ids in due:
        if batch and size + len(anilist_ids) > batch_ids:
            yield batch
            batch, size = [], 0
        batch.append((item_id, anilist_ids))
        size += len(anilist_ids)
    if batch:
        yield batch


def _delete_title(conn: sqlite3.Connection, item_id: str) -> None:
    conn.execute(
        "DELETE FROM enrichment WHERE item_id = ? AND namespace = ? AND source = ?",
        (item_id, NAMESPACE, SOURCE),
    )
    conn.execute(
        "DELETE FROM enrichment_cursor WHERE item_id = ? AND namespace = ? AND source = ?",
        (item_id, NAMESPACE, SOURCE),
    )


def _higher(a: int | None, b: int | None) -> int | None:
    """The higher of two ranks, where NULL means unranked rather than zero."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _write_batch(
    conn: sqlite3.Connection,
    batch: list[tuple[str, list[int]]],
    found: dict[int, list[Tag]],
    now: datetime,
    stats: AniListStats,
    stated: set[tuple[str, str]],
) -> None:
    """Replace every title's rows in one transaction, cursors included, so an
    interrupted sweep never leaves a cursor vouching for rows already deleted."""
    now_iso = now.isoformat(timespec="seconds")
    with conn:
        for item_id, anilist_ids in batch:
            _delete_title(conn, item_id)
            upsert_fetched(conn, NAMESPACE, SOURCE, item_id, now_iso)
            # Every entry the title maps to, merged per stored keyword: the highest
            # rank wins, and a keyword is a spoiler if any copy says so.
            rows: dict[str, tuple[int | None, bool]] = {}
            for tag in (tag for a in anilist_ids for tag in found.get(a, [])):
                keyword = upsert_keyword_form(conn, tag.name)
                if not keyword:
                    continue
                rank, spoiler = tag.rank, tag.spoiler
                if keyword in rows:
                    rank = _higher(rank, rows[keyword][0])
                    spoiler = spoiler or rows[keyword][1]
                rows[keyword] = (rank, spoiler)
                role = role_for(tag.category)
                if role is not None:
                    state_role(conn, keyword, role, SOURCE, now_iso)
                    stated.add((keyword, role))
            for keyword, (rank, spoiler) in rows.items():
                conn.execute(
                    "INSERT INTO enrichment "
                    "(item_id, namespace, source, key, value, fetched_at, rank) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item_id,
                        NAMESPACE,
                        SOURCE,
                        SPOILER_KEY if spoiler else KEYWORD_KEY,
                        keyword,
                        now_iso,
                        rank,
                    ),
                )
                if spoiler:
                    stats.spoiler_keywords_written += 1
                else:
                    stats.keywords_written += 1
            stats.titles_fetched += 1
            if rows:
                stats.titles_matched += 1
