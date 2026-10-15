"""What the Plex DB-EX review page shows and records about synonym keyword pairs.

`keyword_pairs` holds a judge's score for each pair it was asked about (ADR-0018), and a
row for each pair only a person decided (ADR-0020). A person's accept or reject from
the Merges tab or the tag doctor never touches the store, because Plex DB-EX never writes
`plexdb.db`
(ADR-0007, ADR-0017): it goes into `merge_decisions.json`, which `plexdb
fold-merge-decisions` applies at the next sweep. Until then this module lays the
file's decisions over the table, so the page shows a click at once.

The merged and proposed rules live here and nowhere in the schema: merged is
`decision = 'accepted'`, or no decision and `jev_score >= MERGE_AT`; proposed is no
decision and `PROPOSE_AT <= jev_score < MERGE_AT`. A decision outranks the score.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from itertools import product
from typing import Any, ClassVar

from .decisions import DecisionsFile
from .errors import MergeDecisionsError
from .keywords import FILM_SUFFIX_MODEL, KEYWORD_KEY, NAMESPACE, normalize_keyword

__all__ = [
    "MERGE_AT",
    "MERGE_DECISIONS_FILE",
    "PROPOSE_AT",
    "MergeDecisions",
    "NoSuchPair",
    "bucket",
    "decidable",
    "is_stored",
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
    """A decision named a pair with no row and a keyword no title carries."""


class MergeDecisions(DecisionsFile):
    """`merge_decisions.json`: one entry per pair, keyed `(surface_a, surface_b)`, one
    spelling of each keyword."""

    KEY: ClassVar[tuple[str, ...]] = ("surface_a", "surface_b")
    ERROR = MergeDecisionsError
    NOUN = "merge decisions"

    @classmethod
    def key_problem(cls, key: tuple[str, ...]) -> str | None:
        if not all(key):
            return "has an empty surface"
        return None if key[0] != key[1] else "names one surface twice"

    @classmethod
    def resolve(cls, key: tuple[str, ...]) -> tuple[str, ...] | None:
        """The stored pair, in `keyword_a < keyword_b` order; None when a spelling
        normalizes to nothing or both normalize to one value, which is no pair."""
        a, b = (normalize_keyword(surface) for surface in key)
        if not a or not b or a == b:
            return None
        return (a, b) if a < b else (b, a)

    @classmethod
    def spell(
        cls, key: tuple[str, ...], spellings: dict[str, list[str]]
    ) -> Iterator[tuple[str, ...]]:
        return product(*(spellings.get(keyword) or [keyword] for keyword in key))

    def check(self, keyword_a: object, keyword_b: object, decision: object) -> tuple[str, str, str]:
        """The three fields as text, or `ValueError` saying which is wrong."""
        first, second = self.keyword("keyword_a", keyword_a), self.keyword("keyword_b", keyword_b)
        if not first < second:
            raise ValueError("keyword_a must sort before keyword_b")
        return first, second, self.decision(decision)


def bucket(score: float | None, decision: str | None) -> str | None:
    """Where a pair sits: `merged`, `rejected`, `proposed`, or None below the proposal
    band or with no score and no decision."""
    if decision == "accepted":
        return "merged"
    if decision == "rejected":
        return "rejected"
    if score is None:
        return None
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


def is_stored(conn: sqlite3.Connection, keyword: str) -> bool:
    """Whether some title carries `keyword` as a stored keyword value."""
    return (
        conn.execute(
            "SELECT 1 FROM enrichment WHERE namespace = ? AND key = ? AND value = ? LIMIT 1",
            (NAMESPACE, KEYWORD_KEY, keyword),
        ).fetchone()
        is not None
    )


def decidable(conn: sqlite3.Connection, pair: tuple[str, ...]) -> bool:
    """Whether a person's decision on `pair` reaches the table: `keyword_pairs` has a
    row for it, or both keywords are stored values, so the fold writes one (ADR-0020)."""
    found = conn.execute(
        "SELECT 1 FROM keyword_pairs WHERE keyword_a = ? AND keyword_b = ?", pair
    ).fetchone()
    return found is not None or all(is_stored(conn, keyword) for keyword in pair)


def require_pair(conn: sqlite3.Connection, pair: tuple[str, str]) -> None:
    """Raise `NoSuchPair` unless a person may decide `pair` (`decidable`)."""
    if not decidable(conn, pair):
        raise NoSuchPair(pair)


_REVIEW_SQL = (
    "SELECT keyword_a, keyword_b, jev_score, jev_model, decision, decided_at "
    "FROM keyword_pairs WHERE "
)


def _one_pair(
    conn: sqlite3.Connection,
    pair: tuple[str, ...],
    forms: dict[str, str],
    overlay: dict[tuple[str, ...], dict[str, str]],
) -> dict[str, object]:
    """`pair`'s row, or one with no score and no decision when the table has none."""
    found = conn.execute(_REVIEW_SQL + "keyword_a = ? AND keyword_b = ?", pair).fetchone()
    return _row(tuple(found) if found else (*pair, None, None, None, None), forms, overlay)


def review_rows(
    conn: sqlite3.Connection,
    overlay: dict[tuple[str, ...], dict[str, str]],
    forms: dict[str, str],
    pair: tuple[str, str] | None = None,
) -> list[dict[str, object]]:
    """Every pair in a bucket, or the one `pair`, which the caller has checked with
    `require_pair`. A pair no judge scored has `score` and `model` None."""
    if pair is not None:
        return [_one_pair(conn, pair, forms, overlay)]
    rows = conn.execute(
        _REVIEW_SQL + "decision IS NOT NULL OR jev_score >= ?", (PROPOSE_AT,)
    ).fetchall()
    out = [_row(r, forms, overlay) for r in rows]
    # A decision the table has not yet folded in can sit below the band, or name a pair
    # with no row yet. A `cleared` entry on such a pair changes nothing the page lists, and
    # one naming a keyword no title carries never reaches the table, so neither is listed.
    have = {(r["keyword_a"], r["keyword_b"]) for r in out}
    out.extend(
        _one_pair(conn, key, forms, overlay)
        for key, entry in overlay.items()
        if entry["decision"] != "cleared" and key not in have and decidable(conn, key)
    )
    return out
