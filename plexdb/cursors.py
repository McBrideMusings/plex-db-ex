"""Every enrichment writer's `enrichment_cursor` rows.

Each writer keeps a `fetched` cursor per title under its (namespace, source):
the title's facts are current as of that time, and the title leaves the next
run's list until the cursor goes stale. `load_fetched` and `write_fetched`
read and write it; `tmdb_edges` passes its own key per edge type. A writer's
wipe removes its cursors through `delete_cursors`, so every runtime write to
`enrichment_cursor` is in this module.

A writer that spends a request budget per run also keeps an `attempted`
cursor: a run tried the title and got nothing cacheable. An attempted title
moves to the back of the next run's list, so a capped run never spends the
same budget on the same failing titles twice. Cursors are scheduling, never
facts (ADR-0013), so they live in `enrichment_cursor`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime

FETCHED_KEY = "fetched"
ATTEMPTED_KEY = "attempted"

_UPSERT = (
    "INSERT INTO enrichment_cursor (item_id, namespace, source, key, fetched_at) "
    "VALUES (?, ?, ?, ?, ?) "
    "ON CONFLICT(item_id, namespace, source, key) DO UPDATE SET "
    "fetched_at = excluded.fetched_at"
)


def load_fetched(
    conn: sqlite3.Connection, namespace: str, source: str, key: str = FETCHED_KEY
) -> dict[str, str]:
    """item_id -> when its facts were last written, for one writer's cursor key."""
    return {
        row["item_id"]: row["fetched_at"]
        for row in conn.execute(
            "SELECT item_id, fetched_at FROM enrichment_cursor "
            "WHERE namespace = ? AND source = ? AND key = ?",
            (namespace, source, key),
        )
    }


def write_fetched(
    conn: sqlite3.Connection,
    namespace: str,
    source: str,
    item_id: str,
    now_iso: str,
    key: str = FETCHED_KEY,
) -> None:
    """Record a title as fetched at `now_iso` and clear its `attempted` cursor.

    Every writer records a fetch here, capped or not: a writer that never
    marks a title attempted clears nothing, and one that does can never leave
    a stale `attempted` cursor holding a fetched title at the back of its
    order. Runs on the caller's connection without its own transaction, so it
    commits or rolls back together with the facts it vouches for."""
    conn.execute(_UPSERT, (item_id, namespace, source, key, now_iso))
    conn.execute(
        "DELETE FROM enrichment_cursor "
        "WHERE item_id = ? AND namespace = ? AND source = ? AND key = ?",
        (item_id, namespace, source, ATTEMPTED_KEY),
    )


def delete_cursors(
    conn: sqlite3.Connection, namespace: str, source: str, key: str | None = None
) -> int:
    """Delete one writer's cursors — every key, or only `key` — and no other
    writer's. Runs on the caller's connection without its own transaction, so a
    wipe removes cursors and facts together. Returns the number of rows removed."""
    if key is None:
        return conn.execute(
            "DELETE FROM enrichment_cursor WHERE namespace = ? AND source = ?",
            (namespace, source),
        ).rowcount
    return conn.execute(
        "DELETE FROM enrichment_cursor WHERE namespace = ? AND source = ? AND key = ?",
        (namespace, source, key),
    ).rowcount


class Cursors:
    """One writer's cursors, read once at the start of a run."""

    def __init__(self, fetched: dict[str, str], attempted: dict[str, str]) -> None:
        #: item_id -> when its facts were last written.
        self.fetched = fetched
        #: item_id -> when a run last tried it and wrote nothing.
        self.attempted = attempted

    def order_key(self, item_id: str) -> tuple[bool, str]:
        """Sort key putting never-attempted titles first, then attempted ones,
        oldest attempt first. The sort is stable, so the first group keeps
        the order the caller built it in."""
        return (item_id in self.attempted, self.attempted.get(item_id, ""))


def load_cursors(conn: sqlite3.Connection, namespace: str, source: str) -> Cursors:
    cursors = Cursors({}, {})
    for row in conn.execute(
        "SELECT item_id, key, fetched_at FROM enrichment_cursor "
        "WHERE namespace = ? AND source = ? AND key IN (?, ?)",
        (namespace, source, FETCHED_KEY, ATTEMPTED_KEY),
    ):
        target = cursors.fetched if row["key"] == FETCHED_KEY else cursors.attempted
        target[row["item_id"]] = row["fetched_at"]
    return cursors


def mark_attempted(
    conn: sqlite3.Connection, namespace: str, source: str, item_ids: Iterable[str]
) -> None:
    """Move titles that produced nothing cacheable to the back of the next
    run's order. Their `fetched` cursors and their facts are left as they were."""
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    with conn:
        conn.executemany(_UPSERT, [(i, namespace, source, ATTEMPTED_KEY, now) for i in item_ids])
