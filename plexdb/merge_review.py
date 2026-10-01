"""What the Plex TVX review page shows and records about synonym keyword pairs.

`keyword_pairs` holds a judge's score for each pair (ADR-0018). A person's accept or
reject never touches the store, because the explorer never writes `plexdb.db`
(ADR-0007, ADR-0017): it goes into `merge_decisions.json`, which `plexdb
fold-merge-decisions` applies at the next sweep. Until then this module lays the
file's decisions over the table, so the page shows a click at once.

The merged and proposed rules live here and nowhere in the schema: merged is
`decision = 'accepted'`, or no decision and `jev_score >= MERGE_AT`; proposed is no
decision and `PROPOSE_AT <= jev_score < MERGE_AT`. A decision outranks the score.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import MergeDecisionsError

__all__ = [
    "DECISIONS",
    "MERGE_AT",
    "MERGE_DECISIONS_FILE",
    "PROPOSE_AT",
    "MergeDecisions",
    "NoSuchPair",
    "bucket",
    "parse_decisions",
    "require_pair",
    "review_rows",
]

MERGE_DECISIONS_FILE = "merge_decisions.json"
DECISIONS = ("accepted", "rejected", "cleared")

#: A pair Jev scored at least this, with no decision, counts as merged.
MERGE_AT = 0.9
#: A pair scored from here up to `MERGE_AT`, with no decision, waits for a person.
PROPOSE_AT = 0.5


class NoSuchPair(LookupError):
    """A decision named a pair `keyword_pairs` has no row for."""


def parse_decisions(path: Path, text: str) -> list[dict[str, str]]:
    """The file's entries, or `MergeDecisionsError` if any breaks the documented shape."""
    try:
        raw = json.loads(text)
    except ValueError as err:
        raise MergeDecisionsError(f"{path} is not valid JSON: {err}") from None
    if not isinstance(raw, list):
        raise MergeDecisionsError(f"{path} does not hold a list of decisions")
    for index, entry in enumerate(raw):
        where = f"{path} entry {index}"
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(field), str)
            for field in ("keyword_a", "keyword_b", "decision", "decided_at")
        ):
            raise MergeDecisionsError(
                f"{where} needs text keyword_a, keyword_b, decision and decided_at"
            )
        if entry["decision"] not in DECISIONS:
            raise MergeDecisionsError(
                f"{where} has decision {entry['decision']!r}, expected one of {DECISIONS}"
            )
        if not entry["keyword_a"] < entry["keyword_b"]:
            raise MergeDecisionsError(f"{where} must have keyword_a < keyword_b")
        if entry["decision"] != "cleared":
            try:
                datetime.fromisoformat(entry["decided_at"])
            except ValueError:
                raise MergeDecisionsError(
                    f"{where} has decided_at {entry['decided_at']!r}, expected an ISO 8601 time"
                ) from None
    entries: list[dict[str, str]] = raw
    return entries


class MergeDecisions:
    """`merge_decisions.json`, written the way `SavedQueries` writes its file.

    Replaced by rename so a reader never sees half of it, under a lock, and bounded
    because the explorer has no login. Only the last entry per pair counts (the fold
    applies entries in order), so a write keeps one entry per pair and the file's
    size is bounded by the number of pairs the table holds.
    """

    MAX_ENTRIES = 50_000
    MAX_TOTAL_BYTES = 8 << 20
    #: A keyword longer than this cannot be a stored value, so the write refuses it
    #: before looking the pair up.
    KEYWORD_MAX = 200

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def _read(self) -> list[dict[str, str]]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as err:
            raise MergeDecisionsError(f"cannot read {self._path}: {err}") from None
        return parse_decisions(self._path, text)

    def _write(self, body: str) -> None:
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(self._path)
        except OSError as err:
            raise MergeDecisionsError(f"cannot write {self._path}: {err}") from None
        finally:
            tmp.unlink(missing_ok=True)

    def latest(self) -> dict[tuple[str, str], dict[str, str]]:
        """The last entry for each pair."""
        with self._lock:
            return {(e["keyword_a"], e["keyword_b"]): e for e in self._read()}

    def check(self, keyword_a: object, keyword_b: object, decision: object) -> tuple[str, str, str]:
        """The three fields as text, or `ValueError` saying which is wrong."""

        def keyword(name: str, value: object) -> str:
            if not isinstance(value, str) or not value or len(value) > self.KEYWORD_MAX:
                raise ValueError(f"{name} must be text of 1 to {self.KEYWORD_MAX} characters")
            return value

        first, second = keyword("keyword_a", keyword_a), keyword("keyword_b", keyword_b)
        if not first < second:
            raise ValueError("keyword_a must sort before keyword_b")
        if not isinstance(decision, str) or decision not in DECISIONS:
            raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
        return first, second, decision

    def record(self, keyword_a: str, keyword_b: str, decision: str) -> dict[str, str]:
        """Append one decision, dropping the pair's earlier entries; return the entry.

        The caller has checked the fields (`check`) and that the pair has a row.
        """
        entry = {
            "keyword_a": keyword_a,
            "keyword_b": keyword_b,
            "decision": decision,
            "decided_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        with self._lock:
            kept = [
                e
                for e in self._read()
                if (e["keyword_a"], e["keyword_b"]) != (keyword_a, keyword_b)
            ]
            kept.append(entry)
            if len(kept) > self.MAX_ENTRIES:
                raise ValueError(f"too many merge decisions (max {self.MAX_ENTRIES})")
            body = json.dumps(kept, indent=2) + "\n"
            if len(body.encode()) > self.MAX_TOTAL_BYTES:
                raise ValueError(f"merge decisions would exceed {self.MAX_TOTAL_BYTES} bytes")
            self._write(body)
        return entry


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


def _row(
    row: tuple[Any, ...],
    forms: dict[str, str],
    overlay: dict[tuple[str, str], dict[str, str]],
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
    """Raise `NoSuchPair` unless `keyword_pairs` has a row for `pair`."""
    found = conn.execute(
        "SELECT 1 FROM keyword_pairs WHERE keyword_a = ? AND keyword_b = ?", pair
    ).fetchone()
    if found is None:
        raise NoSuchPair(pair)


def review_rows(
    conn: sqlite3.Connection,
    overlay: dict[tuple[str, str], dict[str, str]],
    forms: dict[str, str],
    pair: tuple[str, str] | None = None,
) -> list[dict[str, object]]:
    """Every pair in a bucket, or the one `pair` (raising `NoSuchPair` if it has no row)."""
    sql = (
        "SELECT keyword_a, keyword_b, jev_score, jev_model, decision, decided_at FROM keyword_pairs"
    )
    if pair is None:
        rows = conn.execute(sql + " WHERE decision IS NOT NULL OR jev_score >= ?", (PROPOSE_AT,))
    else:
        rows = conn.execute(sql + " WHERE keyword_a = ? AND keyword_b = ?", pair)
    out = [_row(r, forms, overlay) for r in rows.fetchall()]
    # A decision the table has not yet folded in can sit below the band. A `cleared` entry
    # on such a pair changes nothing the page lists, so it is not looked up.
    if pair is None:
        have = {(r["keyword_a"], r["keyword_b"]) for r in out}
        for key, entry in overlay.items():
            if entry["decision"] != "cleared" and key not in have:
                found = conn.execute(sql + " WHERE keyword_a = ? AND keyword_b = ?", key)
                out.extend(_row(r, forms, overlay) for r in found.fetchall())
    elif not out:
        raise NoSuchPair(pair)
    return out
