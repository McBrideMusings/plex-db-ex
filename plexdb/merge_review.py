"""What the Plex TVX review page shows and records about synonym keyword pairs.

`keyword_pairs` holds a judge's score for each pair (ADR-0018). A person's accept or
reject never touches the store, because Plex TVX never writes `plexdb.db`
(ADR-0007, ADR-0017): it goes into `merge_decisions.json`, which `plexdb
fold-merge-decisions` applies at the next sweep. Until then this module lays the
file's decisions over the table, so the page shows a click at once.

The merged and proposed rules live here and nowhere in the schema: merged is
`decision = 'accepted'`, or no decision and `jev_score >= MERGE_AT`; proposed is no
decision and `PROPOSE_AT <= jev_score < MERGE_AT`. A decision outranks the score.
"""

from __future__ import annotations

import sqlite3
from typing import Any, ClassVar

from .decisions import DecisionsFile
from .errors import MergeDecisionsError
from .keywords import FILM_SUFFIX_MODEL

__all__ = [
    "MERGE_AT",
    "MERGE_DECISIONS_FILE",
    "PROPOSE_AT",
    "MergeDecisions",
    "NoSuchPair",
    "bucket",
    "merge_map",
    "require_pair",
    "review_rows",
]

MERGE_DECISIONS_FILE = "merge_decisions.json"

#: A pair Jev scored at least this, with no decision, counts as merged.
MERGE_AT = 0.9
#: A pair scored from here up to `MERGE_AT`, with no decision, waits for a person.
PROPOSE_AT = 0.5


class NoSuchPair(LookupError):
    """A decision named a pair `keyword_pairs` has no row for."""


class MergeDecisions(DecisionsFile):
    """`merge_decisions.json`: one entry per pair, keyed `(keyword_a, keyword_b)`."""

    KEY: ClassVar[tuple[str, ...]] = ("keyword_a", "keyword_b")
    ERROR = MergeDecisionsError
    NOUN = "merge decisions"

    @classmethod
    def key_problem(cls, key: tuple[str, ...]) -> str | None:
        return None if key[0] < key[1] else "must have keyword_a < keyword_b"

    def check(self, keyword_a: object, keyword_b: object, decision: object) -> tuple[str, str, str]:
        """The three fields as text, or `ValueError` saying which is wrong."""
        first, second = self.keyword("keyword_a", keyword_a), self.keyword("keyword_b", keyword_b)
        if not first < second:
            raise ValueError("keyword_a must sort before keyword_b")
        return first, second, self.decision(decision)


def bucket(score: float, decision: str | None) -> str | None:
    """Where a pair sits: `merged`, `rejected`, `proposed`, or None below the proposal band."""
    if decision == "accepted":
        return "merged"
    if decision == "rejected":
        return "rejected"
    if score >= MERGE_AT:
        return "merged"
    if score >= PROPOSE_AT:
        return "proposed"
    return None


def merge_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Each keyword a merged pair folds away, mapped to the keyword it folds into.

    Merged pairs chain: `a`~`b` and `b`~`c` make one group. A group folds into
    its member the most titles carry (ties by name), so the name a reader sees is
    the one sources use most — except that the "X film" side of a film-suffix
    rule pair never names a group, which folds toward the bare "X" however many
    titles say "X film". A keyword in no merged pair is absent. Nothing in
    `enrichment` changes (ADR-0018); a reader applies this when it reads.
    """
    suffixed = {
        row[0]
        for row in conn.execute(
            "SELECT CASE WHEN keyword_a LIKE '% film' THEN keyword_a ELSE keyword_b END "
            "FROM keyword_pairs WHERE jev_model = ?",
            (FILM_SUFFIX_MODEL,),
        )
    }
    parent: dict[str, str] = {}

    def root(keyword: str) -> str:
        while parent.get(keyword, keyword) != keyword:
            parent[keyword] = parent.get(parent[keyword], parent[keyword])
            keyword = parent[keyword]
        return keyword

    for a, b in conn.execute(
        "SELECT keyword_a, keyword_b FROM keyword_pairs WHERE decision = 'accepted' "
        "OR (decision IS NULL AND jev_score >= ?)",
        (MERGE_AT,),
    ):
        ra, rb = root(a), root(b)
        if ra != rb:
            parent[ra] = rb
    if not parent:
        return {}
    groups: dict[str, list[str]] = {}
    for keyword in list(parent):
        groups.setdefault(root(keyword), []).append(keyword)
    for top in list(groups):
        if top not in groups[top]:
            groups[top].append(top)
    members = sorted({keyword for group in groups.values() for keyword in group})
    carried = dict(
        conn.execute(
            "SELECT value, COUNT(DISTINCT item_id) FROM enrichment "
            "WHERE namespace = 'keywords' AND key = 'keyword' "
            f"AND value IN ({','.join('?' * len(members))}) GROUP BY value",
            members,
        ).fetchall()
    )
    folded: dict[str, str] = {}
    for group in groups.values():
        into = min(group, key=lambda k: (k in suffixed, -carried.get(k, 0), k))
        folded.update({keyword: into for keyword in group if keyword != into})
    return folded


def _row(
    row: tuple[Any, ...],
    forms: dict[str, str],
    overlay: dict[tuple[str, ...], dict[str, str]],
) -> dict[str, object]:
    keyword_a, keyword_b, score, model, decision, decided_at = row
    entry = overlay.get((keyword_a, keyword_b))
    pending = False
    if entry is not None:
        new = None if entry["decision"] == "cleared" else entry["decision"]
        pending = new != decision
        decision = new
        decided_at = None if new is None else entry["decided_at"]
    return {
        "keyword_a": keyword_a,
        "keyword_b": keyword_b,
        "form_a": forms.get(keyword_a, keyword_a),
        "form_b": forms.get(keyword_b, keyword_b),
        "score": score,
        "model": model,
        "decision": decision,
        "decided_at": decided_at,
        "pending": pending,
        "bucket": bucket(score, decision),
    }


def require_pair(conn: sqlite3.Connection, pair: tuple[str, str]) -> None:
    """Raise `NoSuchPair` unless `keyword_pairs` has a scored row for `pair`; a pair
    Jev refused has no score to review."""
    found = conn.execute(
        "SELECT 1 FROM keyword_pairs WHERE keyword_a = ? AND keyword_b = ? AND jev_error IS NULL",
        pair,
    ).fetchone()
    if found is None:
        raise NoSuchPair(pair)


def review_rows(
    conn: sqlite3.Connection,
    overlay: dict[tuple[str, ...], dict[str, str]],
    forms: dict[str, str],
    pair: tuple[str, str] | None = None,
) -> list[dict[str, object]]:
    """Every pair in a bucket, or the one `pair` (raising `NoSuchPair` if it has no row)."""
    sql = (
        "SELECT keyword_a, keyword_b, jev_score, jev_model, decision, decided_at "
        "FROM keyword_pairs WHERE jev_error IS NULL"
    )
    if pair is None:
        rows = conn.execute(sql + " AND (decision IS NOT NULL OR jev_score >= ?)", (PROPOSE_AT,))
    else:
        rows = conn.execute(sql + " AND keyword_a = ? AND keyword_b = ?", pair)
    out = [_row(r, forms, overlay) for r in rows.fetchall()]
    # A decision the table has not yet folded in can sit below the band. A `cleared` entry
    # on such a pair changes nothing the page lists, so it is not looked up.
    if pair is None:
        have = {(r["keyword_a"], r["keyword_b"]) for r in out}
        for key, entry in overlay.items():
            if entry["decision"] != "cleared" and key not in have:
                found = conn.execute(sql + " AND keyword_a = ? AND keyword_b = ?", key)
                out.extend(_row(r, forms, overlay) for r in found.fetchall())
    elif not out:
        raise NoSuchPair(pair)
    return out
