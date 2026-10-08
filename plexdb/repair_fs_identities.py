"""Moving `item_id` values still keyed on the disk mount point (issue #55).

#24 stopped the store *writing* a mount-dependent `item_id`: `PLEX_SOURCE_ROOTS`
is now required, and the `fs:` fallback hashes a title's playback path with
that root stripped off first. It did nothing for the ids already written
before that fix landed — ADR-0008 keeps the walk from ever repointing an
`item_id` once assigned, which is the right rule for an unattended sweep and
exactly why the old ids survive their own fix. `walk`'s own summary line names
the damage on every run: "N title(s) would have derived a different item_id
this walk; kept their existing one" — that count never reaches zero on its
own.

This is the deliberate repair a person points at that known problem, the same
shape as `repair_identities.py` (#23): re-derive every `fs:` item_id with
`identity.derive_item_id` — unmodified — against a canonical path computed the
same way `walk` computes one today, and move the row and everything that
references it to whichever id comes out.

**Only `fs:` ids are ever touched.** Nothing in `identity.PRIORITY`
(`imdb`/`tmdb`/`tvdb`/`plex`) is mount-dependent, so an id in one of those
namespaces can never be wrong for this reason.

**A move can land on an id another `fs:` item already owns.** Two Plex records
with no external GUID that happen to canonicalise to the exact same path — the
same duplicate-across-sections shape #19 already merges for a GUID'd title —
converge on the same corrected id once both are re-derived. That is a
legitimate merge, not a collision: every table below is moved with a
conflict-safe `UPDATE ... WHERE NOT EXISTS`, so a row that would land on a
primary key another row already holds is folded in instead of raised, and the
emptied source `items` row is dropped once nothing points at it any more.

**The rename needs `PRAGMA defer_foreign_keys`.** `items.item_id` is the
primary key every other table's `item_id` (and `edges.from_id`/`to_id`)
references with no `ON UPDATE CASCADE`, so repointing a child to an id that
does not have an `items` row yet — or updating `items` itself while a child
still names the old id — violates the foreign key the instant either
statement runs, in either order. Deferring the check to `COMMIT` lets every
table move independently within one transaction and validates the whole
result once, when every table agrees again.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from . import identity
from .plex_client import PlexSource
from .walk import _SECTION_PASSES, _canonical_for

#: Tables carrying a plain `item_id` column with no other column in its
#: primary key — a move can never collide, so a bare `UPDATE` always carries
#: every row. `plex_items`' key is `rating_key`; `plays`' is `history_key`.
_SIMPLE_TABLES: tuple[str, ...] = ("plex_items", "plays")

#: Tables carrying `item_id` alongside other primary-key columns, mapped to
#: those other columns. A move here can land on a row another row already
#: occupies (the merge case), so it goes through `_move_conflict_safe_table`
#: instead of a bare `UPDATE`.
_PK_COLUMNS: dict[str, tuple[str, ...]] = {
    "external_ids": ("ns", "value", "kind"),
    "enrichment": ("namespace", "source", "key", "value"),
    "enrichment_cursor": ("namespace", "source", "key"),
    "collection_membership": ("collection_id",),
    "keyword_role_statements": ("keyword", "role", "source"),
}


@dataclass
class FsRepairPlan:
    """What a repair pass would do, computed read-only."""

    #: Items currently keyed on an `fs:` id.
    fs_found: int = 0
    #: `old_id -> new_id` for every `fs:` id whose re-derivation differs.
    moves: dict[str, str] = field(default_factory=dict)
    #: `fs:` ids re-derived to the same id already on file — nothing to do.
    unchanged: int = 0
    #: `fs:` ids that could not be re-derived because none of their rating
    #: keys are in the library Plex reports today — the title left Plex
    #: between the walk that wrote it and this repair.
    not_in_plex: int = 0


@dataclass
class ApplyResult:
    """What `apply_fs_repairs` actually did — enough for a caller to verify
    the guarded tables' row counts precisely rather than guessing at a
    threshold.

    `items` and `enrichment` are both allowed to shrink by design (the merge
    case folds two rows into one), so "did this lose data" cannot be answered
    by comparing row counts before and after alone — a caller needs to know
    how much shrinkage this pass itself accounts for, and treat anything past
    that as the real signal.
    """

    #: Rows carried across, per table, summed over every move this pass made.
    #: Tables with nothing carried are omitted rather than reported as zero.
    rows_carried: dict[str, int] = field(default_factory=dict)
    #: How many moves merged into an `items` row `new_id` already held, rather
    #: than renaming `old_id` in place. Each merge removes exactly one `items`
    #: row — the only way this pass ever reduces that table's count.
    merges: int = 0
    #: Per table, how many rows were dropped as an exact duplicate of a fact
    #: `new_id` already carried, during a merge — the only way this pass ever
    #: reduces one of `_PK_COLUMNS`' tables. Tables with nothing dropped are
    #: omitted.
    duplicates_dropped: dict[str, int] = field(default_factory=dict)


@dataclass
class FsRepairStats:
    """What one repair pass found and moved — `FsRepairPlan` plus what
    `apply_fs_repairs` actually carried."""

    fs_found: int = 0
    moved: int = 0
    unchanged: int = 0
    not_in_plex: int = 0
    #: Rows carried across, per table, summed over every move this pass made.
    #: Tables with nothing carried are omitted rather than reported as zero.
    rows_carried: dict[str, int] = field(default_factory=dict)


def fs_item_ids(conn: sqlite3.Connection) -> list[str]:
    """Every identity currently keyed on the `fs:` fallback, sorted for a
    stable report."""
    return sorted(
        row["item_id"]
        for row in conn.execute("SELECT item_id FROM items WHERE item_id LIKE 'fs:%'")
    )


def _canonical_by_rating_key(source: PlexSource, source_roots: Sequence[str]) -> dict[str, str]:
    """Every rating key Plex currently holds, and the canonical string
    `identity.derive_item_id`'s path-hash fallback would hash for it today.

    Enumerated the same way `walk_all` enumerates — section by section, one
    pass per kind — because `_canonical_for` needs the same record shape the
    walk reads. Built once per repair pass rather than once per item, since
    the same enumeration serves every `fs:` item in the store.
    """
    canonical: dict[str, str] = {}
    for section in source.sections():
        passes = _SECTION_PASSES.get(section.type)
        if passes is None:
            continue
        for _kind, plex_type in passes:
            for record in source.items(section.key, plex_type):
                rating_key = record.get("ratingKey")
                if rating_key:
                    canonical[str(rating_key)] = _canonical_for(record, source_roots)
    return canonical


def _plan_moves(
    conn: sqlite3.Connection,
    item_ids: Sequence[str],
    canonical_by_rating_key: dict[str, str],
) -> tuple[dict[str, str], int]:
    """`{old_id: new_id}` for every `fs:` id whose re-derivation differs, and
    how many could not be re-derived at all.

    An `fs:` id maps to one rating key almost always — nothing merges two
    GUID-less titles onto one id, since they carry no external id for the
    walk's `_resolve_existing` to match on. Several rating keys on one `fs:`
    id can still happen (issue #19's same-file-two-sections case), so every
    rating key on the id is tried and the lowest one present in the library
    today is used, which keeps the choice deterministic across runs.

    External ids are read from the store rather than assumed empty: an `fs:`
    id means none of `identity.PRIORITY`'s namespaces were usable *the last
    time this ran*, which `derive_item_id` will confirm again here — reading
    the real row is what makes this a re-derivation and not a guess.
    """
    moves: dict[str, str] = {}
    not_in_plex = 0
    for old_id in item_ids:
        rating_keys = sorted(
            row["rating_key"]
            for row in conn.execute(
                "SELECT rating_key FROM plex_items WHERE item_id = ?", (old_id,)
            )
        )
        canonical = next(
            (canonical_by_rating_key[rk] for rk in rating_keys if rk in canonical_by_rating_key),
            None,
        )
        if canonical is None:
            not_in_plex += 1
            continue
        external_ids = [
            (row["ns"], row["value"])
            for row in conn.execute(
                "SELECT ns, value FROM external_ids WHERE item_id = ?", (old_id,)
            )
        ]
        new_id = identity.derive_item_id(external_ids, canonical)
        if new_id != old_id:
            moves[old_id] = new_id
    return moves, not_in_plex


def plan_fs_repairs(
    conn: sqlite3.Connection, source: PlexSource, source_roots: Sequence[str] = ()
) -> FsRepairPlan:
    """Work out what a repair pass would move, without writing anything.

    Read-only, so a caller can decide whether a move is coming — and so
    whether a backup is worth taking — before opening a write transaction.
    """
    item_ids = fs_item_ids(conn)
    if not item_ids:
        return FsRepairPlan()
    canonical = _canonical_by_rating_key(source, source_roots)
    moves, not_in_plex = _plan_moves(conn, item_ids, canonical)
    unchanged = len(item_ids) - len(moves) - not_in_plex
    return FsRepairPlan(
        fs_found=len(item_ids), moves=moves, unchanged=unchanged, not_in_plex=not_in_plex
    )


def _move_simple_table(conn: sqlite3.Connection, table: str, old_id: str, new_id: str) -> int:
    """Repoint `table.item_id` from `old_id` to `new_id`. See `_SIMPLE_TABLES`
    for why this can never collide."""
    cursor = conn.execute(f"UPDATE {table} SET item_id = ? WHERE item_id = ?", (new_id, old_id))
    return cursor.rowcount


def _move_conflict_safe_table(
    conn: sqlite3.Connection, table: str, old_id: str, new_id: str
) -> tuple[int, int]:
    """Repoint `table.item_id` from `old_id` to `new_id`, folding a row into
    one `new_id` already holds rather than colliding with it.

    `new_id` already having a row here is the merge case: two `fs:` items
    converged on the same corrected id, so both may have independently
    recorded the same fact (the same enrichment key, the same collection
    membership). Whichever copy already sits under `new_id` is kept; the
    `old_id` copy is folded in only where nothing conflicts, and any leftover
    duplicate is dropped rather than raised — it names the identical fact the
    `new_id` copy already carries, so nothing is lost by dropping it.

    Returns `(moved, dropped)`: `dropped` is exactly the row count of the
    trailing `DELETE`, so a caller can account for this table's shrinkage
    precisely rather than guessing at it from a before/after total.
    """
    pk_columns = _PK_COLUMNS[table]
    match = " AND ".join(f"t2.{col} = {table}.{col}" for col in pk_columns)
    move_cursor = conn.execute(
        f"UPDATE {table} SET item_id = ? "
        f"WHERE item_id = ? "
        f"  AND NOT EXISTS (SELECT 1 FROM {table} t2 WHERE {match} AND t2.item_id = ?)",
        (new_id, old_id, new_id),
    )
    moved = move_cursor.rowcount
    drop_cursor = conn.execute(f"DELETE FROM {table} WHERE item_id = ?", (old_id,))
    return moved, drop_cursor.rowcount


def _move_edges(conn: sqlite3.Connection, old_id: str, new_id: str) -> int:
    """Repoint every edge naming `old_id`, as either endpoint, to `new_id`.

    Handled separately from `_PK_COLUMNS`: `edges` has two columns that can
    hold an item_id, not one, and its primary key
    `(from_id, to_id, edge_type)` spans both.
    """
    moved = 0
    for column, other in (("from_id", "to_id"), ("to_id", "from_id")):
        cursor = conn.execute(
            f"UPDATE edges SET {column} = ? "
            f"WHERE {column} = ? "
            f"  AND NOT EXISTS ("
            f"      SELECT 1 FROM edges e2 WHERE e2.{column} = ? "
            f"        AND e2.{other} = edges.{other} AND e2.edge_type = edges.edge_type"
            f"  )",
            (new_id, old_id, new_id),
        )
        moved += cursor.rowcount
    conn.execute("DELETE FROM edges WHERE from_id = ? OR to_id = ?", (old_id, old_id))
    return moved


def _move_item(
    conn: sqlite3.Connection,
    old_id: str,
    new_id: str,
    totals: Counter[str],
    dropped: Counter[str],
) -> bool:
    """Move one identity's row and everything that references it. Returns
    whether this move merged into an existing `items` row rather than
    renaming `old_id` in place.

    `items` is moved last: whether `new_id` already has a row there is what
    decides rename versus merge, and every other table has already been
    repointed by the time that question is asked, so it reads the state the
    move itself is about to create rather than a stale snapshot.
    """
    for table in _SIMPLE_TABLES:
        totals[table] += _move_simple_table(conn, table, old_id, new_id)
    for table in _PK_COLUMNS:
        moved, table_dropped = _move_conflict_safe_table(conn, table, old_id, new_id)
        totals[table] += moved
        dropped[table] += table_dropped
    totals["edges"] += _move_edges(conn, old_id, new_id)

    exists = conn.execute("SELECT 1 FROM items WHERE item_id = ?", (new_id,)).fetchone()
    if exists is None:
        conn.execute("UPDATE items SET item_id = ? WHERE item_id = ?", (new_id, old_id))
        return False
    conn.execute("DELETE FROM items WHERE item_id = ?", (old_id,))
    return True


def apply_fs_repairs(conn: sqlite3.Connection, plan: FsRepairPlan) -> ApplyResult:
    """Carry out `plan.moves`, and report exactly what moved, merged, and was
    dropped as a duplicate.

    One transaction for the whole pass, with `PRAGMA defer_foreign_keys = ON`
    so a row can land on an id that has no `items` row of its own yet without
    the foreign key check failing mid-move — it is checked once, at
    `COMMIT`, by which point every table agrees. Explicit `BEGIN`/`commit`/
    `rollback` rather than `with conn:`, because the pragma only takes effect
    inside an already-open transaction and `sqlite3` does not open one ahead
    of a bare `PRAGMA` statement.

    Sorted iteration so two runs over the same pending moves apply them in
    the same order, which matters only for the merge case: which of two
    converging `fs:` ids survives as the `items` row is otherwise arbitrary,
    and a stable order keeps that choice reproducible.
    """
    if not plan.moves:
        return ApplyResult()
    totals: Counter[str] = Counter()
    dropped: Counter[str] = Counter()
    merges = 0
    conn.execute("BEGIN")
    try:
        conn.execute("PRAGMA defer_foreign_keys = ON")
        for old_id in sorted(plan.moves):
            if _move_item(conn, old_id, plan.moves[old_id], totals, dropped):
                merges += 1
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    return ApplyResult(
        rows_carried={table: count for table, count in totals.items() if count},
        merges=merges,
        duplicates_dropped={table: count for table, count in dropped.items() if count},
    )


def repair_fs_identities(
    conn: sqlite3.Connection, source: PlexSource, source_roots: Sequence[str] = ()
) -> FsRepairStats:
    """Plan and apply in one call — the shape most tests and the `--dry-run`-
    less command path want. The command itself calls `plan_fs_repairs` and
    `apply_fs_repairs` separately so it can decide whether a backup is
    warranted before writing anything.
    """
    plan = plan_fs_repairs(conn, source, source_roots)
    result = apply_fs_repairs(conn, plan)
    return FsRepairStats(
        fs_found=plan.fs_found,
        moved=len(plan.moves),
        unchanged=plan.unchanged,
        not_in_plex=plan.not_in_plex,
        rows_carried=result.rows_carried,
    )
