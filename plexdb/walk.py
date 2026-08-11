"""Walking a Plex library into `items`, `external_ids`, and the rating-key map.

ADR-0005: the store walks Plex itself, because watch history only carries a
Plex `ratingKey` and the Plex-internal `plex://` agent GUID — the weakest
tier in `identity.PRIORITY` — so the external GUIDs an `item_id` is actually
built from exist nowhere else until something walks the library and records
them.

**Idempotent, on purpose.** Two invariants make a second walk over unchanged
input a no-op: `items`/`external_ids` are upserted by `item_id`, and once a
title has been recorded against an `item_id`, that mapping is never
repointed at a differently-derived id on a later walk — see `_resolve_existing`
and `_write_item` for why. Both are required by issue #3's acceptance
criteria; neither is enforced by the schema itself, only by this module.

**ADR-0008's amendment**: an existing identity is found by any of the
title's external ids first (in `identity.PRIORITY` order, so the pick is
deterministic when several are already recorded), then by the Plex rating
key, so the title keeps one identity through both a wholesale GUID
re-match (caught by rating key) and a remove-and-re-add that changes the
rating key but not the GUIDs (caught by external id).

**ADR-0008's second amendment** (issue #23): an external id is only ever
matched within its own media kind. TMDB and TVDB number movies, shows and
episodes in separate lists that all start at 1, so a bare `tmdb://1678`
means *Godzilla* on a movie and *The Golden Girls* on a show. Matching
across kinds handed the show the movie's identity and fused two unrelated
titles into one row.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, NamedTuple

from . import identity
from .plex_client import PLEX_TYPE_EPISODE, PLEX_TYPE_MOVIE, PLEX_TYPE_SHOW, PlexSource, Section

#: Plex section `type` -> the `(items row type, Plex item type)` passes to run
#: over it, in order. A show section is read twice, shows before episodes, so
#: an episode's `show_item_id` resolves against a show written moments earlier
#: in the same pass. A section type absent from this table (a photo library, a
#: music library) is skipped rather than guessed at.
_SECTION_PASSES: dict[str, tuple[tuple[str, int], ...]] = {
    "movie": (("movie", PLEX_TYPE_MOVIE),),
    "show": (("show", PLEX_TYPE_SHOW), ("episode", PLEX_TYPE_EPISODE)),
}


@dataclass
class WalkStats:
    """What one walk touched — the summary `plexdb walk` prints on completion."""

    sections_walked: int = 0
    titles_seen: int = 0
    titles_written: int = 0
    #: Titles with no recognised external GUID, landed under the `fs:` fallback.
    fallback_to_path: int = 0
    #: Titles resolved to an existing identity by Plex rating key — a
    #: wholesale GUID re-match, rating key unchanged — where the existing
    #: item_id was kept rather than forking a second row. Zero on a library
    #: nothing has re-matched since the last pass.
    identity_kept_on_guid_change: int = 0
    #: Titles resolved to an existing identity by one of their external ids
    #: — a remove-and-re-add that changed the rating key but not the GUIDs,
    #: or a GUID set that gained an id already recorded elsewhere — where
    #: the existing item_id was kept rather than forking a second row. Zero
    #: on a library nothing has re-matched since the last pass.
    identity_kept_by_external_id: int = 0


def _guid_pairs(raw_guids: list[dict[str, Any]] | None) -> list[tuple[str, str]]:
    """Plex's `Guid` array -> `(namespace, value)` pairs, in Plex's own order.

    A record with no recognised GUID at all reports `Guid` as absent or
    `null`, not an empty list — both are handled the same way here, and
    `derive_item_id` (unmodified, ADR-0002) is what decides namespace
    priority and what counts as a usable value.
    """
    pairs: list[tuple[str, str]] = []
    for guid in raw_guids or []:
        raw_id = guid.get("id")
        if not raw_id or "://" not in raw_id:
            continue
        namespace, _, value = raw_id.partition("://")
        pairs.append((namespace, value))
    return pairs


def _raw_path(record: dict[str, Any]) -> str | None:
    """The playback path of the first `Media`/`Part` a record carries, if any.

    Present on a movie or an episode; absent on a show, which is a container
    with no file of its own.
    """
    for media in record.get("Media") or []:
        for part in media.get("Part") or []:
            file_path = part.get("file")
            if file_path:
                return str(file_path)
    return None


def _canonical_for(record: dict[str, Any], source_roots: Sequence[str]) -> str:
    """The string `derive_item_id`'s path-hash fallback hashes for this record.

    A movie or episode's playback path, canonicalised. A show carries no
    file of its own, so its Plex-internal `key` (e.g.
    `/library/metadata/81044`) stands in — the only string Plex gives a show
    that is both stable across walks and unique to it. This only matters for
    a show with no recognised external GUID at all, which real libraries
    rarely produce (a show is almost always matched to at least a TVDB id).
    """
    raw_path = _raw_path(record)
    if raw_path is not None:
        return identity.canonical_path(raw_path, source_roots)
    key = record.get("key") or f"/library/metadata/{record.get('ratingKey', '')}"
    return identity.canonical_path(str(key), source_roots)


class _Resolved(NamedTuple):
    """An identity this store already holds for a title, and which key found it."""

    item_id: str
    found_by: Literal["external_id", "rating_key"]


def _resolve_existing(
    external_ids: list[tuple[str, str]],
    rating_key: str,
    kind: str,
    existing_by_external_id: dict[tuple[str, str, str], str],
    existing_by_rating_key: dict[str, str],
) -> _Resolved | None:
    """The already-recorded identity for this title, if any — external id
    first, then rating key (ADR-0008's amendment).

    External ids are tried in `identity.PRIORITY` order, the same order
    `derive_item_id` uses, so the pick is deterministic when a title carries
    several and they are already recorded against different identities: the
    first namespace in priority order with a hit wins, regardless of which
    id Plex listed first. Only if no external id is already known does the
    rating key decide.

    An external id matches only within its own `kind` (issue #23). TMDB and
    TVDB number movies, shows and episodes in separate lists that all start
    at 1, so `tmdb 1678` is a movie *and* an unrelated show; matching across
    kinds fused the two into one identity.

    `None` when neither resolves — this store has never seen the title under
    any id it currently carries.
    """
    for namespace in identity.PRIORITY:
        for ns, value in external_ids:
            if ns != namespace or not value.strip():
                continue
            matched = existing_by_external_id.get((ns, value, kind))
            if matched is not None:
                return _Resolved(matched, "external_id")
    prior_id = existing_by_rating_key.get(rating_key)
    if prior_id is not None:
        return _Resolved(prior_id, "rating_key")
    return None


def _write_item(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    *,
    kind: str,
    section: Section,
    source_roots: Sequence[str],
    now: str,
    stats: WalkStats,
    existing_by_rating_key: dict[str, str],
    existing_by_external_id: dict[tuple[str, str, str], str],
    show_item_ids: dict[str, str],
) -> None:
    """Upsert one Plex record as an `items` row, its `external_ids`, and its
    `plex_items` mapping. A record Plex returned with no `ratingKey` is not
    addressable, so it is skipped rather than written under a made-up key.
    """
    raw_rating_key = record.get("ratingKey")
    if not raw_rating_key:
        return
    rating_key = str(raw_rating_key)
    stats.titles_seen += 1

    external_ids = _guid_pairs(record.get("Guid"))
    canonical = _canonical_for(record, source_roots)
    derived_id = identity.derive_item_id(external_ids, canonical)
    if derived_id.startswith("fs:"):
        stats.fallback_to_path += 1

    resolved = _resolve_existing(
        external_ids, rating_key, kind, existing_by_external_id, existing_by_rating_key
    )
    if resolved is not None and resolved.item_id != derived_id:
        # Either this rating key's GUID set changed since the last walk
        # (Plex re-matched it, or it gained a GUID it previously lacked —
        # caught by the rating key), or the rating key itself changed while
        # an external id stayed the same (a remove-and-re-add — caught by
        # the external id). Adopting the freshly derived id here would leave
        # the old items/external_ids rows behind as an orphaned second row
        # for the same physical title — the silent fork the walk must not
        # produce. Keeping the identity it already has is the other of the
        # two acceptable outcomes the issue names; every occurrence is
        # counted by which key retained it, so it is visible in the walk
        # summary rather than silent.
        item_id = resolved.item_id
        if resolved.found_by == "external_id":
            stats.identity_kept_by_external_id += 1
        else:
            stats.identity_kept_on_guid_change += 1
    else:
        item_id = derived_id

    # `titleSort` and `studio` are read the same way for every kind: Plex
    # reports no `titleSort` on an episode (so this is `None` there anyway),
    # and does report `studio` on an episode — inherited from its show.
    title_sort = record.get("titleSort")
    studio = record.get("studio")

    if kind == "episode":
        show_title = record.get("grandparentTitle")
        season = record.get("parentIndex")
        episode = record.get("index")
        grandparent_rating_key = record.get("grandparentRatingKey")
        show_item_id = (
            None
            if grandparent_rating_key is None
            else show_item_ids.get(str(grandparent_rating_key))
        )
    else:
        show_title = None
        season = None
        episode = None
        show_item_id = None

    conn.execute(
        """
        INSERT INTO items
            (item_id, type, title, title_sort, show_title, show_item_id,
             season, episode, year, duration_ms, content_rating, studio)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            type = excluded.type,
            title = excluded.title,
            title_sort = excluded.title_sort,
            show_title = excluded.show_title,
            show_item_id = excluded.show_item_id,
            season = excluded.season,
            episode = excluded.episode,
            year = excluded.year,
            duration_ms = excluded.duration_ms,
            content_rating = excluded.content_rating,
            studio = excluded.studio
        """,
        (
            item_id,
            kind,
            record.get("title") or "",
            title_sort,
            show_title,
            show_item_id,
            season,
            episode,
            record.get("year"),
            record.get("duration"),
            record.get("contentRating"),
            studio,
        ),
    )

    for namespace, value in external_ids:
        # (ns, value, kind) is the primary key. On conflict, keep whichever
        # item_id claimed it first rather than repointing it — the same
        # never-repoint-silently rule `_resolve_existing` enforces above,
        # applied to one external id instead of the whole title. `kind` is in
        # the key because a TMDB or TVDB number is unique only inside one
        # media type (issue #23).
        conn.execute(
            "INSERT INTO external_ids (item_id, ns, value, kind) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (ns, value, kind) DO NOTHING",
            (item_id, namespace, value, kind),
        )

    conn.execute(
        """
        INSERT INTO plex_items (rating_key, item_id, section_id, last_seen)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(rating_key) DO UPDATE SET
            item_id = excluded.item_id,
            section_id = excluded.section_id,
            last_seen = excluded.last_seen
        """,
        (rating_key, item_id, section.key, now),
    )

    existing_by_rating_key[rating_key] = item_id
    for namespace, value in external_ids:
        # Mirrors the DB's own `ON CONFLICT (ns, value) DO NOTHING` above:
        # first claim wins. Without this, a second title later in this same
        # pass that shares one of these external ids but was not yet in the
        # store when this walk started would miss the match — it would only
        # exist in `external_ids` from a moment ago, not in the snapshot
        # `existing_by_external_id` was preloaded from — and fork instead
        # of resolving to this identity.
        existing_by_external_id.setdefault((namespace, value, kind), item_id)
    if kind == "show":
        # Recorded for the episode pass over this same section, which runs
        # next and resolves each episode's `show_item_id` from here.
        show_item_ids[rating_key] = item_id
    stats.titles_written += 1


def walk_all(
    conn: sqlite3.Connection,
    source: PlexSource,
    source_roots: Sequence[str] = (),
    *,
    section_key: str | None = None,
) -> WalkStats:
    """Walk every section `source` reports (or just `section_key`) into `conn`.

    One transaction for the whole pass: a failure partway through leaves the
    store exactly as it was before the walk started, never half-written.
    `_SECTION_PASSES` decides which sections are walked and in what order
    their records are read.
    """
    stats = WalkStats()
    now = datetime.now(UTC).isoformat(timespec="seconds")

    existing_by_rating_key: dict[str, str] = {
        row["rating_key"]: row["item_id"]
        for row in conn.execute("SELECT rating_key, item_id FROM plex_items")
    }
    existing_by_external_id: dict[tuple[str, str, str], str] = {
        (row["ns"], row["value"], row["kind"]): row["item_id"]
        for row in conn.execute("SELECT ns, value, kind, item_id FROM external_ids")
    }
    show_item_ids: dict[str, str] = {
        row["rating_key"]: row["item_id"]
        for row in conn.execute(
            "SELECT p.rating_key AS rating_key, p.item_id AS item_id "
            "FROM plex_items p JOIN items i ON i.item_id = p.item_id "
            "WHERE i.type = 'show'"
        )
    }

    with conn:
        for section in source.sections():
            if section_key is not None and section.key != section_key:
                continue
            passes = _SECTION_PASSES.get(section.type)
            if passes is None:
                continue  # a section type this walk does not understand (e.g. "photo")
            stats.sections_walked += 1

            for kind, plex_type in passes:
                for record in source.items(section.key, plex_type):
                    _write_item(
                        conn,
                        record,
                        kind=kind,
                        section=section,
                        source_roots=source_roots,
                        now=now,
                        stats=stats,
                        existing_by_rating_key=existing_by_rating_key,
                        existing_by_external_id=existing_by_external_id,
                        show_item_ids=show_item_ids,
                    )

    return stats
