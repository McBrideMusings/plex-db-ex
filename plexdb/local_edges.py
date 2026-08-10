"""Deriving affinity edges from Plex collection co-membership.

Every title Plex places in the same collection is treated as related to
every other title in that collection — both directions, one edge per ordered
pair. This is the store's highest-weight relationship signal (issue #7): a
human built the collection, no API call is spent finding it, and because the
input (`items`, plus Plex's own collection listing) is already available,
the whole set recomputes on every sweep rather than being cached against
staleness the way `tmdb_edges.py`'s external sources are.

**Only non-`smart` collections count as curation.** Plex's `smart` flag
marks a collection as a saved filter (e.g. "everything released in the last
30 days") rather than a set a person actually assembled — the issue's own
framing ("the collections a human actually curated") is exactly the
distinction that flag encodes, so a smart collection is skipped rather than
treated as curation. Confirmed live against a real library: a smart
collection reports `smart` as the string `"1"`; a regular one omits the key
entirely (`None`), never a JSON boolean — `_is_smart` handles both, plus the
`"0"`/`0` a scripted fixture might use.

**A single edge type, a constant rank.** Unlike TMDB's `recommendations` and
`similar`, a Plex collection carries no per-pair ordering to store —  two
titles either share a collection or they don't. `rank` is fixed at `1` for
every row; any weighting a consumer wants to apply on top is explicitly out
of scope for this store (issue #7's "Out of scope" section).

**Wholesale replace, not per-title.** `tmdb_edges.py` replaces one title's
`(from_id, edge_type)` set at a time, because each title is a separate,
independently-cacheable API call. Local edges have no per-title fetch to
cache — the whole `local_collection` edge type is recomputed from Plex's
current collection listings in one pass. Every Plex call this needs happens
*before* the transaction that replaces the set opens, so a Plex failure
partway through a fetch never touches a row already on disk — the previous
set survives untouched, the same guarantee an interrupted `tmdb_edges.py`
sweep gives per title, just scoped to the whole edge type here rather than
one title's set.

**Only a member already in `items` produces an edge.** Same rule as
`tmdb_edges.py`, and the same reason (ADR-0009): a collection can hold a
title this store has never walked (a different section, an unmatched item),
and there is no id to point an edge at until a walk gives it one. Such a
member is counted (`members_skipped_not_in_library`) and dropped, never
invented.

**Large collections are reported, never capped.** Measured against a real
library: several hand-curated thematic lists (a "Letterboxd" export, an
awards-season shelf) run into the hundreds or low thousands of members, and
turn into a near-quadratic number of edges (N members -> up to N*(N-1)
directed edges). Issue #7 explicitly defers the cap-or-exclude decision
until real numbers exist, so this module only reports — `report_threshold`
(default `DEFAULT_REPORT_THRESHOLD`) decides what shows up in the summary,
and every edge from a large collection still lands.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, NamedTuple, Protocol

from .plex_client import Section

#: The one edge type this module writes.
LOCAL_COLLECTION_EDGE_TYPE = "local_collection"

#: Every local edge carries this rank — Plex gives no per-pair ordering to
#: store verbatim (unlike TMDB's array position), and weighting one local
#: edge over another is a consumer decision, explicitly out of scope for
#: this store (issue #7).
_RANK = 1

#: A collection at or above this many resolvable members is flagged in the
#: summary as worth a second look. Measured against a real library: a
#: hand-built franchise or themed shelf is almost always well under this;
#: several hundred-plus-member thematic lists exist too, and this constant
#: exists only to surface them for the future cap-or-exclude decision issue
#: #7 explicitly defers — it caps nothing on its own.
DEFAULT_REPORT_THRESHOLD = 50


class CollectionSource(Protocol):
    """The read surface `refresh_local_edges` needs — real or recorded.

    Three methods, all read-only: the section list (same shape
    `plex_client.PlexSource` uses), one section's collection listing, and
    one collection's member records — so a recorded fixture and a live
    response are interchangeable here too.
    """

    def sections(self) -> list[Section]:
        """Every library section the server reports, in Plex's own order."""
        ...

    def collections(self, section_key: str) -> list[dict[str, Any]]:
        """Every collection Plex lists for one section — smart and regular
        alike; `smart` is filtered by the caller, not here."""
        ...

    def collection_children(self, collection_key: str) -> list[dict[str, Any]]:
        """The member records of one collection, addressed by its own
        `ratingKey`."""
        ...


class LargeCollection(NamedTuple):
    """One collection whose resolvable membership met `report_threshold`."""

    title: str
    member_count: int


@dataclass
class LocalEdgeStats:
    """What one sweep touched — the summary `plexdb local-edges` prints."""

    #: Every collection Plex listed, smart and regular alike.
    collections_seen: int = 0
    #: A saved-search collection (`smart`), skipped as not-actually-curated.
    collections_skipped_smart: int = 0
    #: Non-smart collections whose membership was resolved into (possibly
    #: zero) edges — `collections_seen - collections_skipped_smart`.
    collections_processed: int = 0
    edges_written: int = 0
    #: A collection member with no matching row in `plex_items` — not yet
    #: walked, or living in a section this sweep does not visit. Counted and
    #: dropped, never given an invented id (same rule as `tmdb_edges.py`,
    #: ADR-0009).
    members_skipped_not_in_library: int = 0
    large_collections: list[LargeCollection] = field(default_factory=list)


def _is_smart(record: dict[str, Any]) -> bool:
    """Plex marks an auto-generated saved-search collection with `smart=1`
    — confirmed live as the string `"1"`, with the key absent (`None`) on a
    regular collection. Every truthy spelling except `"0"`/`0` counts as
    smart, so a scripted fixture using a JSON boolean or bare `0`/`1` still
    resolves correctly."""
    raw = record.get("smart")
    if raw is None:
        return False
    if isinstance(raw, str):
        return raw not in ("", "0")
    return bool(raw)


def _resolve_members(
    members: list[dict[str, Any]], rating_key_to_item: dict[str, str]
) -> tuple[list[str], int]:
    """One collection's member records -> the local `item_id`s they resolve to,
    in Plex's own order, each appearing exactly once.

    Returns `(item_ids, skipped_not_in_library)`. A member with no matching
    `plex_items` row is counted in the second value and dropped rather than
    given an invented id (ADR-0009). A member Plex lists twice — or two rating
    keys landing on the same `item_id` — collapses to one entry, so the pair
    loop downstream can never produce a duplicate row or a self-edge.
    """
    item_ids: list[str] = []
    skipped = 0
    for member in members:
        rating_key = member.get("ratingKey")
        if not rating_key:
            continue
        item_id = rating_key_to_item.get(str(rating_key))
        if item_id is None:
            skipped += 1
            continue
        item_ids.append(item_id)
    return list(dict.fromkeys(item_ids)), skipped


def refresh_local_edges(
    conn: sqlite3.Connection,
    source: CollectionSource,
    *,
    report_threshold: int = DEFAULT_REPORT_THRESHOLD,
) -> LocalEdgeStats:
    """Recompute every `local_collection` edge from Plex's current collection
    listings.

    Two phases, deliberately kept apart. Phase one only reads Plex and this
    store's own `plex_items` map, resolving every non-smart collection into
    a list of local `item_id`s — no row is touched yet, so a `PlexError`
    here leaves the previous edge set exactly as it was. Phase two, inside
    one transaction, deletes the whole `local_collection` edge type and
    reinserts it collection by collection: pairs are generated and inserted
    with `INSERT OR IGNORE` rather than accumulated into one Python-side set
    first, so peak memory is bounded by the single largest collection's
    N*(N-1) pairs rather than the sum across every collection — a real
    library measurement in issue #7's development showed that sum can run
    into the tens of millions.
    """
    stats = LocalEdgeStats()
    rating_key_to_item: dict[str, str] = {
        row["rating_key"]: row["item_id"]
        for row in conn.execute("SELECT rating_key, item_id FROM plex_items")
    }

    memberships: list[list[str]] = []
    for section in source.sections():
        if section.type not in ("movie", "show"):
            continue
        for collection in source.collections(section.key):
            stats.collections_seen += 1
            if _is_smart(collection):
                stats.collections_skipped_smart += 1
                continue
            stats.collections_processed += 1

            collection_key = collection.get("ratingKey")
            if not collection_key:
                continue

            item_ids, skipped = _resolve_members(
                source.collection_children(str(collection_key)), rating_key_to_item
            )
            stats.members_skipped_not_in_library += skipped

            if len(item_ids) >= report_threshold:
                title = collection.get("title") or ""
                stats.large_collections.append(LargeCollection(title, len(item_ids)))
            # A collection with fewer than two resolvable members has no pair
            # to produce, so it never reaches the write phase at all.
            if len(item_ids) >= 2:
                memberships.append(item_ids)

    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    with conn:
        conn.execute("DELETE FROM edges WHERE edge_type = ?", (LOCAL_COLLECTION_EDGE_TYPE,))
        for item_ids in memberships:
            conn.executemany(
                "INSERT OR IGNORE INTO edges (from_id, to_id, edge_type, rank, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    (from_id, to_id, LOCAL_COLLECTION_EDGE_TYPE, _RANK, now_iso)
                    for from_id in item_ids
                    for to_id in item_ids
                    if from_id != to_id
                ),
            )
        stats.edges_written = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE edge_type = ?", (LOCAL_COLLECTION_EDGE_TYPE,)
        ).fetchone()[0]

    return stats
