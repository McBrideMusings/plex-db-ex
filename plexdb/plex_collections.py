"""Harvesting Plex's own hand-built collections into collection membership.

A collection someone assembled in Plex is a crowd list with a crowd of one,
and it lands in the same two tables MDBList does (`collection`,
`collection_membership`) under `source = 'plex'`. It is the store's
highest-confidence membership signal — a human put those titles together, and
no API call outside Plex is spent finding it.

**This used to write pairwise edges, and that was the bug** (issue #48). Every
title in a collection was edged to every other title, both directions, so a
collection of N members produced N*(N-1) rows. Measured against a real
library: 154 curated collections holding **19,365 memberships between them**
expanded to **17,809,980 edges**, a 920x blow-up that took the store from
72 MiB to 3.9 GB and the published snapshot — the file every consumer opens
(ADR-0007) — to 3.7 GB. One collection was most of it: *Oscars Death Race
Forever* has 3,005 members and alone produced 9,027,020 pairs.

Co-membership is a linear fact. "This title is in that collection" is one row;
the pairs are that fact multiplied out, and multiplying it out is what cost
920x. The `collection` and `collection_membership` tables added for MDBList
(issue #34) already hold it in the linear form, and `source` is a plain column
precisely so a new source is a new value rather than a migration. A consumer
asking "what shares a collection with X" joins through membership instead of
reading a stored expansion of that join.

**No cap, and none needed.** The old module reported large collections so a
future cap-or-exclude decision would have real numbers. That decision is now
moot rather than deferred: a 3,005-member collection writes 3,005 rows, which
is not a size worth capping.

**Only non-`smart` collections count as curation.** Plex's `smart` flag marks
a saved filter ("everything released in the last 30 days") rather than a set a
person assembled. Confirmed live: a smart collection reports `smart` as the
string `"1"`; a regular one omits the key entirely (`None`), never a JSON
boolean — `_is_smart` handles both, plus the `"0"`/`0` a scripted fixture
might use.

**Wholesale replace, not per-collection.** Unlike MDBList, whose lists are
staleness-gated behind a rate-limited API, this input is already local: the
whole `plex` source is recomputed from Plex's current listings in one pass.
Every Plex call happens *before* the write transaction opens, so a Plex
failure partway through leaves the previous set exactly as it was.

**Only a member already in `items` is stored.** Same rule as every other
source, and the same reason (ADR-0009): a collection can hold a title this
store has never walked, and there is no id to point at until a walk gives it
one. Such a member is counted and dropped, never invented — and
`collection.size` still records the collection's full length, so a consumer
can see how much of it the library holds.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .plex_client import Section

#: The value this module writes to `collection.source`.
PLEX_SOURCE = "plex"


class CollectionSource(Protocol):
    """The read surface `refresh_plex_collections` needs — real or recorded.

    Three methods, all read-only: the section list (same shape
    `plex_client.PlexSource` uses), one section's collection listing, and one
    collection's member records — so a recorded fixture and a live response
    are interchangeable here too.
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


@dataclass
class PlexCollectionStats:
    """What one sweep touched — the summary the command prints."""

    #: Every collection Plex listed, smart and regular alike.
    collections_seen: int = 0
    #: A saved-search collection (`smart`), skipped as not-actually-curated.
    collections_skipped_smart: int = 0
    #: Non-smart collections written — `collections_seen - skipped_smart`.
    collections_written: int = 0
    memberships_written: int = 0
    #: A collection member with no matching row in `plex_items` — not yet
    #: walked, or living in a section this sweep does not visit. Counted and
    #: dropped, never given an invented id (ADR-0009).
    members_skipped_not_in_library: int = 0


def _is_smart(record: dict[str, Any]) -> bool:
    """Plex marks an auto-generated saved-search collection with `smart=1` —
    confirmed live as the string `"1"`, with the key absent (`None`) on a
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
    keys landing on the same `item_id` — collapses to one entry, so the
    membership table's `(collection_id, item_id)` key can never be violated.
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


@dataclass(frozen=True)
class _Resolved:
    """One collection, read from Plex and resolved against this store."""

    collection_id: str
    name: str
    #: The collection's full length as Plex reports it, including members this
    #: library has not walked — so a consumer can tell a fully-held collection
    #: from a tenth of one.
    size: int
    item_ids: list[str]


def refresh_plex_collections(
    conn: sqlite3.Connection,
    source: CollectionSource,
) -> PlexCollectionStats:
    """Recompute every `plex`-sourced collection from Plex's current listings.

    Two phases, deliberately kept apart. Phase one only reads Plex and this
    store's own `plex_items` map — no row is touched, so a `PlexError` here
    leaves the previous set exactly as it was. Phase two, inside one
    transaction, replaces the whole `plex` source.
    """
    stats = PlexCollectionStats()
    rating_key_to_item: dict[str, str] = {
        row["rating_key"]: row["item_id"]
        for row in conn.execute("SELECT rating_key, item_id FROM plex_items")
    }

    resolved: list[_Resolved] = []
    for section in source.sections():
        if section.type not in ("movie", "show"):
            continue
        for collection in source.collections(section.key):
            stats.collections_seen += 1
            if _is_smart(collection):
                stats.collections_skipped_smart += 1
                continue

            collection_key = collection.get("ratingKey")
            if not collection_key:
                continue

            members = source.collection_children(str(collection_key))
            item_ids, skipped = _resolve_members(members, rating_key_to_item)
            stats.members_skipped_not_in_library += skipped
            resolved.append(
                _Resolved(
                    collection_id=f"{PLEX_SOURCE}:{collection_key}",
                    name=str(collection.get("title") or f"Plex collection {collection_key}"),
                    size=len(members),
                    item_ids=item_ids,
                )
            )

    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    with conn:
        # Memberships first and explicitly: `PRAGMA foreign_keys` is not
        # guaranteed on for this connection, so relying on the cascade would
        # leave orphans wherever it happens to be off.
        conn.execute(
            "DELETE FROM collection_membership WHERE collection_id IN "
            "(SELECT collection_id FROM collection WHERE source = ?)",
            (PLEX_SOURCE,),
        )
        conn.execute("DELETE FROM collection WHERE source = ?", (PLEX_SOURCE,))
        for entry in resolved:
            # `url` and `likes` stay NULL: a Plex collection has no public
            # address and no follower count, and inventing one would make the
            # column mean two different things across sources (ADR-0012).
            conn.execute(
                "INSERT INTO collection "
                "(collection_id, source, name, url, size, likes, observed_at) "
                "VALUES (?, ?, ?, NULL, ?, NULL, ?)",
                (entry.collection_id, PLEX_SOURCE, entry.name, entry.size, now_iso),
            )
            # `rank` stays NULL because Plex publishes no ordering within a
            # collection — two titles are both in it or they are not. NULL is
            # the honest answer; a constant 1, which the old edge shape used,
            # says "ranked first" to anything reading it.
            conn.executemany(
                "INSERT INTO collection_membership "
                "(collection_id, item_id, rank, mentions, observed_at) "
                "VALUES (?, ?, NULL, NULL, ?)",
                ((entry.collection_id, item_id, now_iso) for item_id in entry.item_ids),
            )
            stats.collections_written += 1
            stats.memberships_written += len(entry.item_ids)

    return stats
