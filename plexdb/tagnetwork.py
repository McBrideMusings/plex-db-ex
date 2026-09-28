"""The writer's half of the tag explorer's default tag network.

`plexdb.explore` draws the Graph view's network of every tag by title
co-membership, which takes seconds to minutes. With no noise excluded that
network is a pure function of the keyword rows, so this module draws it once
and stores it in `tag_network`, `tag_network_edge` and `tag_network_state`;
the explorer serves the stored copy while its fingerprint still matches the
keyword rows and draws live when it does not. This is the only code that
writes those tables (ADR-0001).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from .explore import KINDS, network_fingerprint, tag_network


@dataclass(frozen=True)
class Refreshed:
    kind: str
    #: False when the stored network already matched the keyword rows.
    redrawn: bool
    #: Nodes placed on the network, from the stored copy when it was kept.
    nodes: int
    seconds: float


def refresh_tag_networks(conn: sqlite3.Connection) -> list[Refreshed]:
    """Bring the stored default tag network of every kind up to date.

    A kind whose stored fingerprint equals the current one is left alone.
    Every other kind is drawn first, with no write lock held, and then all
    the redrawn kinds replace their rows in one transaction, so a reader sees
    either the old networks or the new ones and a failure part-way leaves the
    old ones in place.
    """
    drawn = []
    results: list[Refreshed] = []
    for kind in KINDS:
        fingerprint = network_fingerprint(conn, kind)
        state = conn.execute(
            "SELECT fingerprint FROM tag_network_state WHERE kind = ?", (kind,)
        ).fetchone()
        if state is not None and state[0] == fingerprint:
            (nodes,) = conn.execute(
                "SELECT COUNT(*) FROM tag_network WHERE kind = ?", (kind,)
            ).fetchone()
            results.append(Refreshed(kind, False, nodes, 0.0))
            continue
        started = time.monotonic()
        net = tag_network(conn, kind)
        seconds = time.monotonic() - started
        drawn.append((kind, fingerprint, net))
        results.append(Refreshed(kind, True, len(net.nodes), seconds))

    if drawn:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with conn:
            for kind, fingerprint, net in drawn:
                conn.execute("DELETE FROM tag_network WHERE kind = ?", (kind,))
                conn.execute("DELETE FROM tag_network_edge WHERE kind = ?", (kind,))
                conn.executemany(
                    "INSERT INTO tag_network (kind, value, df, x, y) VALUES (?, ?, ?, ?, ?)",
                    [(kind, n.value, n.df, n.x, n.y) for n in net.nodes],
                )
                conn.executemany(
                    "INSERT INTO tag_network_edge (kind, a, b, shared) VALUES (?, ?, ?, ?)",
                    [(kind, a, b, shared) for a, b, shared in net.edges],
                )
                conn.execute(
                    "INSERT INTO tag_network_state (kind, fingerprint, computed_at) "
                    "VALUES (?, ?, ?) ON CONFLICT (kind) DO UPDATE SET "
                    "fingerprint = excluded.fingerprint, computed_at = excluded.computed_at",
                    (kind, fingerprint, now),
                )
    return results
