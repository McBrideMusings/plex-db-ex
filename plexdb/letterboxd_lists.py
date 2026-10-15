"""Harvesting Letterboxd lists into `collection`, `collection_entry` and
`collection_membership` — the second crowd-list source after MDBList.

Letterboxd has no list feed, so this harvest fetches only the lists a person added by URL
(`list_decisions.json`, folded into `list_decision`) and the Letterboxd lists already
stored, never a list discovered on its own. Everything after fetching is the MDBList
harvest's own procedure (`collections.py`): the same gating by staleness, `refetch`,
`retry` and the three-failure stop; the same removal of an excluded or removed list; the
same whole-list snapshot transaction. A list is written whole or not at all.

**An entry's identity is its TMDB id, scoped by media type.** A list page names a film by
slug only, so each film's page is read once per run for the `data-tmdb-id` and
`data-tmdb-type` on its `<body>` (`letterboxd_client.parse_film_ids`); `tv` becomes `show`,
the kind the library's own TMDB ids carry. A film Letterboxd has not matched to TMDB is
stored under `('letterboxd', <slug>, 'movie')`: it can never resolve to a library title,
but the list still names it.

**A failure writes nothing.** A Cloudflare challenge, a page without the list marker, a
film page that will not load, or fewer entries read than the list says it holds records
a `list_attempt` row with the reason and leaves the stored list as it was.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from .collections import (
    ADDED,
    LETTERBOXD_SOURCE,
    HarvestStats,
    ListEntry,
    apply_removals,
    decisions_for,
    gate,
    place_entries,
    record_failure,
    resolve_index,
    write_snapshot,
)
from .errors import LetterboxdError
from .letterboxd_client import FilmIds, LetterboxdListSource
from .list_decisions import canonical_list_url, letterboxd_list_parts
from .staleness import DEFAULT_STALE_DAYS

#: The most entries one list may hold and still be harvested. Each entry costs a film
#: page request and about a second, so a list past this is refused whole instead of
#: holding a sweep for hours.
MAX_LIST_ENTRIES = 2000

#: Lists in a row that fail before the run stops. Letterboxd challenging this client, or
#: not answering, fails every list the same way, and asking for the rest only burns each
#: one's `MAX_FAILURES` attempts.
MAX_CONSECUTIVE_FAILURES = 3

#: Films between progress lines while a long list resolves.
_PROGRESS_EVERY = 100


def _collection_id(user: str, slug: str) -> str:
    return f"{LETTERBOXD_SOURCE}:{user}/{slug}"


def refresh_letterboxd_lists(
    conn: sqlite3.Connection,
    source: LetterboxdListSource,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> HarvestStats:
    """Refresh every Letterboxd list a person added, and every one already stored, into
    `collection`, `collection_entry` and `collection_membership`.

    Every list prints one log line: its URL, the pages and entries read, how many resolved
    to the library, and the outcome — `written`, `cached`, `stopped`, or `failed` with the
    reason.
    """
    cutoff = datetime.now(UTC) - timedelta(days=stale_days)
    stats = HarvestStats()

    decisions = decisions_for(conn, LETTERBOXD_SOURCE)
    excluded = apply_removals(conn, decisions, stats, LETTERBOXD_SOURCE)
    attempts = {row["list_key"]: row for row in conn.execute("SELECT * FROM list_attempt")}
    index = resolve_index(conn)
    stocked = {
        row["collection_id"]
        for row in conn.execute("SELECT DISTINCT collection_id FROM collection_entry")
    }
    stored_rows = {
        canonical: row
        for row in conn.execute(
            "SELECT collection_id, url, observed_at, provenance FROM collection "
            "WHERE source = ? AND url IS NOT NULL",
            (LETTERBOXD_SOURCE,),
        )
        if (canonical := canonical_list_url(row["url"]))
    }
    added = {url for url, row in decisions.items() if row["added"]}
    keys = sorted((added | set(stored_rows)) - excluded)
    stats.lists_excluded = len(excluded)

    films: dict[str, FilmIds] = {}
    failures_in_a_row = 0
    for key in keys:
        if failures_in_a_row >= MAX_CONSECUTIVE_FAILURES:
            print(
                f"letterboxd: {failures_in_a_row} lists in a row failed; "
                "leaving the rest for the next run",
                flush=True,
            )
            break
        stats.lists_seen += 1
        parts = letterboxd_list_parts(key)
        assert parts is not None
        user, slug = parts
        collection_id = _collection_id(user, slug)
        held_row = stored_rows.get(key)
        fetch, held = gate(
            conn,
            source=LETTERBOXD_SOURCE,
            key=key,
            label=key,
            decision=decisions.get(key),
            held=attempts.get(key),
            seen_at=held_row["observed_at"] if held_row else None,
            stocked=collection_id in stocked,
            cutoff=cutoff,
            stats=stats,
        )
        if not fetch:
            continue

        try:
            contents = source.list_contents(user, slug)
            if len(contents.entries) > MAX_LIST_ENTRIES:
                raise LetterboxdError(
                    f"the list holds {len(contents.entries)} entries, over the "
                    f"{MAX_LIST_ENTRIES} this harvest resolves"
                )
            if contents.size is not None and len(contents.entries) < contents.size:
                raise LetterboxdError(
                    f"read {len(contents.entries)} of the {contents.size} entries the list "
                    "says it holds"
                )
            entries: list[ListEntry] = []
            for done, film in enumerate(contents.entries, start=1):
                if film.slug not in films:
                    films[film.slug] = source.film_ids(film.slug)
                if done % _PROGRESS_EVERY == 0:
                    print(
                        f"letterboxd: {key} -> {done} of {len(contents.entries)} films resolved",
                        flush=True,
                    )
                ids = films[film.slug]
                external = ("tmdb", ids.tmdb_id) if ids.tmdb_id else (LETTERBOXD_SOURCE, film.slug)
                entries.append(ListEntry((external,), ids.media_type, film.title, film.year))
        except LetterboxdError as err:
            print(f"letterboxd: {key} -> failed: {err}", flush=True)
            record_failure(
                conn, key, held, datetime.now(UTC).isoformat(timespec="seconds"), str(err)
            )
            stats.lists_failed += 1
            failures_in_a_row += 1
            continue
        failures_in_a_row = 0

        stored, memberships = place_entries(index, entries, stats)
        write_snapshot(
            conn,
            collection_id=collection_id,
            source=LETTERBOXD_SOURCE,
            name=contents.name,
            url=key,
            size=contents.size if contents.size is not None else len(contents.entries),
            likes=None,
            provenance=ADDED if key in added else (held_row["provenance"] if held_row else ADDED),
            list_key=key,
            fetched_iso=datetime.now(UTC).isoformat(timespec="seconds"),
            stored=stored,
            memberships=memberships,
        )
        stats.lists_fetched += 1
        stats.entries_parsed += len(contents.entries)
        stats.entries_stored += len(stored)
        stats.memberships_written += len(memberships)
        print(
            f"letterboxd: {key} -> HTTP 200, {contents.pages} page(s), "
            f"{len(contents.entries)} entries parsed, {len(memberships)} resolved to the "
            "library, written",
            flush=True,
        )
    return stats
