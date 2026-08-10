"""Reconciling this store's `item_id` against `etv-station`'s `entry_id`.

Both derive identity by the same first-hit-wins rule (ADR-0002), from two
independent implementations — Python here, Rust in `etv-station`
(`crates/etv-station/src/catalog/identity.rs`) — kept in agreement only by a
shared test fixture (ADR-0006). Nothing has ever compared the two derivations
over a real library, where the awkward inputs actually live. This module is
that comparison. It is a report, not a fix: whatever it finds becomes its own
issue (issue #5).

**The join key is the Plex `ratingKey`.** Both stores record every physical
Plex item's rating key as raw provenance, independent of whatever identity
each side derived for it: `plex_items.rating_key` here, and
`entry_sources.source_id` where `source = 'plex'` in `etv-station`'s
`catalog.db`. Matching on it is what lets this module tell "the two stores
derived different ids for the same title" apart from "the two stores don't
know about the same titles" — and it only makes sense when both stores were
built by walking the *same* Plex server, which nothing in either schema
records, so this module cannot check it.

**Read-only on both sides, always.** Both connections are handed in by the
caller, already opened read-only (`store.open_readonly`); nothing here writes
a statement, so a write attempted through either connection raises on its
own rather than being prevented by this module's own discipline.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import identity

#: Recognised GUID namespaces, in derivation-priority order. Only these can
#: decide which id a title derives, so only these matter to a mismatch's
#: reason. This store records every namespace Plex reports in `external_ids`
#: (`walk._guid_pairs` does not filter); `etv-station`'s Plex ingester keeps
#: only these four (`ingest/plex.rs::parse_guid`). Restricting the comparison
#: to this set is what keeps that difference in *storage* from being
#: misreported as a difference in *derivation*.
_RECOGNISED_NS = frozenset(identity.PRIORITY)


@dataclass(frozen=True)
class Mismatch:
    """One title where the two stores derived different identities."""

    rating_key: str
    title: str
    item_id: str
    entry_id: str
    reason: str


@dataclass(frozen=True)
class ReconcileReport:
    """What comparing every title both stores know about found.

    `only_in_plexdb` and `only_in_etv` are `(rating_key, title)` pairs, sorted
    by rating key — same ordering discipline as `mismatches`, so two runs
    over unchanged data produce byte-identical output.
    """

    compared: int
    agree: int
    mismatches: tuple[Mismatch, ...]
    only_in_plexdb: tuple[tuple[str, str], ...]
    only_in_etv: tuple[tuple[str, str], ...]

    @property
    def mismatched(self) -> int:
        return len(self.mismatches)


def _classify(
    item_id: str,
    entry_id: str,
    plexdb_guids: frozenset[tuple[str, str]],
    etv_guids: frozenset[tuple[str, str]],
) -> str:
    """Why `item_id` and `entry_id` disagree for one title.

    Both sides run the same rule, so a mismatch traces back to the *inputs*
    the two walks recorded differing — except the one case this function
    names explicitly: identical recognised GUID sets on both sides that still
    produced different winners, which would mean the two implementations have
    actually drifted, not just the data they were fed.
    """
    ns_p, _, val_p = item_id.partition(":")
    ns_e, _, val_e = entry_id.partition(":")

    if ns_p == "fs" and ns_e == "fs":
        return (
            "both fell back to a path hash, but the hashes differ — the canonical "
            "path disagreed between the two walks (different source roots, or one "
            "side used a fallback key the other didn't)"
        )
    if ns_p == "fs" or ns_e == "fs":
        if ns_p == "fs":
            fell_back, kept, winning_ns = "plex-db-ex", "etv-station", ns_e
        else:
            fell_back, kept, winning_ns = "etv-station", "plex-db-ex", ns_p
        return (
            f"{kept} recorded a {winning_ns} GUID for this title that {fell_back} does "
            f"not have, so {fell_back} fell back to a path hash"
        )
    if ns_p == ns_e:
        return (
            f"both derived a {ns_p} id but the values disagree ({val_p!r} vs {val_e!r}) "
            "— the two walks likely ran against different Plex data for this title"
        )
    etv_has_plexdbs_ns = any(ns == ns_p for ns, _ in etv_guids)
    plexdb_has_etvs_ns = any(ns == ns_e for ns, _ in plexdb_guids)
    if not etv_has_plexdbs_ns:
        return (
            f"a {ns_p} GUID is present in plex-db-ex but absent from etv-station for "
            f"this title, so etv-station fell through to its next-best GUID ({ns_e})"
        )
    if not plexdb_has_etvs_ns:
        return (
            f"a {ns_e} GUID is present in etv-station but absent from plex-db-ex for "
            f"this title, so plex-db-ex fell through to its next-best GUID ({ns_p})"
        )
    return (
        f"both stores recorded a {ns_p} and a {ns_e} GUID for this title but picked "
        "different ones — the derivation rule itself disagrees, not the data it was fed"
    )


def reconcile(plexdb_conn: sqlite3.Connection, etv_conn: sqlite3.Connection) -> ReconcileReport:
    """Compare every title both stores know about, joined by Plex rating key."""
    plexdb_by_rk: dict[str, str] = {
        row["rating_key"]: row["item_id"]
        for row in plexdb_conn.execute("SELECT rating_key, item_id FROM plex_items")
    }
    etv_by_rk: dict[str, str] = {
        row["source_id"]: row["entry_id"]
        for row in etv_conn.execute(
            "SELECT source_id, entry_id FROM entry_sources WHERE source = 'plex'"
        )
    }
    plexdb_titles: dict[str, str] = {
        row["item_id"]: row["title"]
        for row in plexdb_conn.execute("SELECT item_id, title FROM items")
    }
    etv_titles: dict[str, str] = {
        row["entry_id"]: row["title"]
        for row in etv_conn.execute("SELECT entry_id, title FROM entries")
    }

    placeholders = ",".join("?" for _ in _RECOGNISED_NS)
    plexdb_guids: dict[str, set[tuple[str, str]]] = {}
    for row in plexdb_conn.execute(
        f"SELECT item_id, ns, value FROM external_ids WHERE ns IN ({placeholders})",
        tuple(_RECOGNISED_NS),
    ):
        plexdb_guids.setdefault(row["item_id"], set()).add((row["ns"], row["value"]))
    etv_guids: dict[str, set[tuple[str, str]]] = {}
    for row in etv_conn.execute(
        "SELECT entry_id, namespace, value FROM entry_external_ids "
        f"WHERE namespace IN ({placeholders})",
        tuple(_RECOGNISED_NS),
    ):
        etv_guids.setdefault(row["entry_id"], set()).add((row["namespace"], row["value"]))

    common = sorted(set(plexdb_by_rk) & set(etv_by_rk))
    agree = 0
    mismatches: list[Mismatch] = []
    for rating_key in common:
        item_id = plexdb_by_rk[rating_key]
        entry_id = etv_by_rk[rating_key]
        if item_id == entry_id:
            agree += 1
            continue
        title = plexdb_titles.get(item_id) or etv_titles.get(entry_id) or ""
        reason = _classify(
            item_id,
            entry_id,
            frozenset(plexdb_guids.get(item_id, ())),
            frozenset(etv_guids.get(entry_id, ())),
        )
        mismatches.append(Mismatch(rating_key, title, item_id, entry_id, reason))

    only_in_plexdb = tuple(
        sorted(
            (rk, plexdb_titles.get(plexdb_by_rk[rk], ""))
            for rk in set(plexdb_by_rk) - set(etv_by_rk)
        )
    )
    only_in_etv = tuple(
        sorted((rk, etv_titles.get(etv_by_rk[rk], "")) for rk in set(etv_by_rk) - set(plexdb_by_rk))
    )

    return ReconcileReport(
        compared=len(common),
        agree=agree,
        mismatches=tuple(mismatches),
        only_in_plexdb=only_in_plexdb,
        only_in_etv=only_in_etv,
    )
