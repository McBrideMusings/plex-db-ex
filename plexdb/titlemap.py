"""The writer's half of the tag explorer's default title map.

`plexdb.explore` draws a map of every title by keyword similarity, which takes
seconds to minutes. With no noise excluded that map is a pure function of the
keyword rows, so this module draws it once and stores it in `title_map` and
`title_map_state`; the explorer serves the stored copy while its fingerprint
still matches the keyword rows and draws live when it does not. This is the only
code that writes those tables (ADR-0001).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from .explore import KINDS, keyword_fingerprint, title_map


@dataclass(frozen=True)
class Refreshed:
    kind: str
    #: False when the stored map already matched the keyword rows.
    redrawn: bool
    #: Titles placed on the map, from the stored copy when it was kept.
    placed: int
    seconds: float


def refresh_title_maps(conn: sqlite3.Connection) -> list[Refreshed]:
    """Bring the stored default map of every kind up to date.

    A kind whose stored fingerprint equals the current one is left alone. Every
    other kind is drawn first, with no write lock held, and then all the redrawn
    kinds replace their rows in one transaction, so a reader sees either the old
    maps or the new ones and a failure part-way leaves the old ones in place.
    """
    drawn = []
    results: list[Refreshed] = []
    for kind in KINDS:
        fingerprint = keyword_fingerprint(conn, kind)
        state = conn.execute(
            "SELECT fingerprint FROM title_map_state WHERE kind = ?", (kind,)
        ).fetchone()
        if state is not None and state[0] == fingerprint:
            (placed,) = conn.execute(
                "SELECT COUNT(*) FROM title_map WHERE kind = ?", (kind,)
            ).fetchone()
            results.append(Refreshed(kind, False, placed, 0.0))
            continue
        started = time.monotonic()
        tmap = title_map(conn, kind)
        seconds = time.monotonic() - started
        drawn.append((kind, fingerprint, tmap))
        results.append(Refreshed(kind, True, len(tmap.points), seconds))

    if drawn:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with conn:
            for kind, fingerprint, tmap in drawn:
                conn.execute("DELETE FROM title_map WHERE kind = ?", (kind,))
                conn.executemany(
                    "INSERT INTO title_map (kind, item_id, x, y) VALUES (?, ?, ?, ?)",
                    [(kind, p.item_id, p.x, p.y) for p in tmap.points],
                )
                conn.execute(
                    "INSERT INTO title_map_state (kind, fingerprint, unplaced, computed_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (kind) DO UPDATE SET "
                    "fingerprint = excluded.fingerprint, unplaced = excluded.unplaced, "
                    "computed_at = excluded.computed_at",
                    (kind, fingerprint, tmap.unplaced, now),
                )
    return results
