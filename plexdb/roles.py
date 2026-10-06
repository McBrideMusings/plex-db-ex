"""Asking Jev what roles each stored keyword plays (ADR-0019).

Every distinct stored keyword with no `source = 'jev'` row in `keyword_roles` is
asked about once: one request carrying one `noul` question per role in
`keywords.ROLES`, about the keyword's shortest readable surface form
(`keywords.readable_surfaces`). Each answer becomes one row, five per keyword,
with `score` exactly as Jev gave it and `model` the model id in the response.

A keyword that has a `jev` row is never asked again, so a rerun with no new
keyword sends nothing. A keyword Jev refuses with a 400 or 422 gets five rows
with `error` set and `score` and `model` NULL, so it is not asked again and does
not hold up its batch. Any other failure (429 exhausted, 5xx, a bad key, no
connection) stops the run.

Keywords are asked about in batches of `KEYWORD_BATCH`, each judged completely
before its rows are written in one transaction, so a run killed partway keeps
every finished batch and the next run asks only about keywords still without a
row. Nothing here decides whether a keyword *has* a role: that threshold is the
reader's.

**Decisions.** Plex TVX never writes `plexdb.db` (ADR-0007, ADR-0017), so a
person's accept or reject reaches `keyword_role_decisions` only through
`role_decisions.json`, which `fold_role_decisions` applies early in a sweep.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import Config
from .decisions import FoldStats, decisions_dir
from .errors import JevRejected
from .jev_client import RoleJudge, RoleVerdict, judge_all
from .keywords import ROLES, readable_surfaces
from .role_review import ROLE_DECISIONS_FILE, RoleDecisions, has_verdict

__all__ = ["RoleStats", "fold_role_decisions", "judge_keyword_roles", "role_decisions_path"]

#: This judge's source name in `keyword_roles`.
SOURCE = "jev"
#: Keywords judged per committed batch — 1,000 rows.
KEYWORD_BATCH = 200
#: Jev requests in flight at once.
JUDGE_WORKERS = 8


@dataclass
class RoleStats:
    keywords_stored: int = 0
    #: Keywords that already had a `jev` row, left alone.
    keywords_already_judged: int = 0
    keywords_judged: int = 0
    #: Keywords Jev refused with a 400 or 422, stored with `error` and no score.
    keywords_unjudgeable: int = 0
    rows_written: int = 0
    batches_committed: int = 0


def judge_keyword_roles(
    conn: sqlite3.Connection,
    judge: RoleJudge,
    *,
    limit: int | None = None,
    log: Callable[[str], None] = lambda _line: None,
) -> RoleStats:
    """Ask `judge` about every keyword with no `jev` row and write five rows each.

    `limit` caps how many keywords this run asks about, so a first run over a
    whole vocabulary can be taken in pieces.
    """
    stats = RoleStats()
    surfaces = readable_surfaces(conn)
    stats.keywords_stored = len(surfaces)
    judged = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT keyword FROM keyword_roles WHERE source = ?", (SOURCE,)
        )
    }
    todo = sorted(keyword for keyword in surfaces if keyword not in judged)
    stats.keywords_already_judged = stats.keywords_stored - len(todo)
    if limit is not None:
        todo = todo[:limit]
    log(
        f"{len(todo):,} keyword(s) to judge; "
        f"{stats.keywords_already_judged:,} of {stats.keywords_stored:,} already judged"
    )

    for start in range(0, len(todo), KEYWORD_BATCH):
        batch = todo[start : start + KEYWORD_BATCH]
        verdicts = _judge_all(judge, [surfaces[keyword] for keyword in batch])
        stated_at = datetime.now(UTC).isoformat(timespec="seconds")
        with conn:
            for keyword, verdict in zip(batch, verdicts, strict=True):
                if isinstance(verdict, JevRejected):
                    stats.rows_written += _write(conn, keyword, stated_at, error=str(verdict))
                    stats.keywords_unjudgeable += 1
                    log(f"unjudgeable keyword {surfaces[keyword]!r}: {verdict}")
                    continue
                stats.rows_written += _write(conn, keyword, stated_at, verdict=verdict)
                stats.keywords_judged += 1
        stats.batches_committed += 1
        log(
            f"{start + len(batch):,}/{len(todo):,} keyword(s) judged, "
            f"{stats.rows_written:,} row(s) written"
        )
    return stats


def _write(
    conn: sqlite3.Connection,
    keyword: str,
    stated_at: str,
    *,
    verdict: RoleVerdict | None = None,
    error: str | None = None,
) -> int:
    """One row per role: Jev's score and model, or the refusal with neither."""
    written = 0
    for role in ROLES:
        written += conn.execute(
            "INSERT INTO keyword_roles (keyword, role, source, score, model, error, stated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (
                keyword,
                role,
                SOURCE,
                verdict.scores[role] if verdict else None,
                verdict.model if verdict else None,
                error,
                stated_at,
            ),
        ).rowcount
    return written


def _judge_all(judge: RoleJudge, tags: list[str]) -> list[RoleVerdict | JevRejected]:
    """Every tag's verdict, in order; see `jev_client.judge_all`."""
    return judge_all(judge.judge_roles, tags, JUDGE_WORKERS)


def role_decisions_path(config: Config) -> Path:
    """`role_decisions.json`, beside Plex TVX's saved-queries file and `merge_decisions.json`."""
    return decisions_dir(config) / ROLE_DECISIONS_FILE


def fold_role_decisions(conn: sqlite3.Connection, path: Path) -> FoldStats:
    """Apply `path` to `keyword_role_decisions`; a missing file is no decisions.

    The file is a list of `{keyword, role, decision, decided_at}`, where `decision`
    is `accepted`, `rejected` or `cleared`. Entries apply in list order, so the last
    entry for a (keyword, role) wins. `accepted` and `rejected` set the row's
    `decision` and `decided_at`; `cleared` deletes the row, so both read NULL. An
    entry naming a (keyword, role) with no verdict in `keyword_roles` is counted and
    ignored. The whole file is validated before the first row changes, and applied
    in one transaction. It is applied every run, so the file stays the record of the
    latest decision per (keyword, role).
    """
    stats = FoldStats()
    entries = RoleDecisions.load(path)
    if entries is None:
        return stats
    stats.file_found = True
    stats.entries = len(entries)
    with conn:
        for entry in entries:
            key = (entry["keyword"], entry["role"])
            if not has_verdict(conn, *key):
                stats.unmatched += 1
                continue
            stats.matched += 1
            if entry["decision"] == "cleared":
                conn.execute(
                    "DELETE FROM keyword_role_decisions WHERE keyword = ? AND role = ?", key
                )
            else:
                conn.execute(
                    "INSERT INTO keyword_role_decisions (keyword, role, decision, decided_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (keyword, role) DO UPDATE SET "
                    "decision = excluded.decision, decided_at = excluded.decided_at",
                    (*key, entry["decision"], entry["decided_at"]),
                )
    return stats
