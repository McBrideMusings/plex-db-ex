"""What the Plex TVX Roles tab shows and records about keyword roles.

`keyword_roles` holds what each source states and each judge scores about a keyword's
role, and `keyword_role_decisions` what a person ruled (ADR-0019). A person's accept or
reject never touches the store, because Plex TVX never writes `plexdb.db` (ADR-0007,
ADR-0017): it goes into `role_decisions.json`, which `plexdb fold-role-decisions`
applies at the next sweep. Until then this module lays the file's decisions over the
table, so the page shows a click at once.

Nothing here decides whether a keyword has a role (ADR-0012): the tab shows every
source's score as stored and the person's decision beside it.

A judge's refusal (`error` set) is no verdict, so every read here keeps `error IS NULL`;
a (keyword, role) with only a refusal is not listed and cannot be decided.
"""

from __future__ import annotations

import sqlite3
from typing import ClassVar

from .decisions import DecisionsFile
from .errors import RoleDecisionsError
from .keywords import ROLES

__all__ = [
    "LIST_LIMIT",
    "ROLE_DECISIONS_FILE",
    "NoSuchRole",
    "RoleDecisions",
    "has_verdict",
    "require_role",
    "role_cell",
    "roles_json",
]

ROLE_DECISIONS_FILE = "role_decisions.json"

#: Keywords one listing returns at most; the filter reaches the rest.
LIST_LIMIT = 200

_Overlay = dict[tuple[str, ...], dict[str, str]]


class NoSuchRole(LookupError):
    """A decision named a (keyword, role) `keyword_roles` has no verdict for."""


class RoleDecisions(DecisionsFile):
    """`role_decisions.json`: one entry per (keyword, role)."""

    KEY: ClassVar[tuple[str, ...]] = ("keyword", "role")
    ERROR = RoleDecisionsError
    NOUN = "role decisions"

    @classmethod
    def key_problem(cls, key: tuple[str, ...]) -> str | None:
        keyword, role = key
        if not keyword:
            return "has an empty keyword"
        if role not in ROLES:
            return f"has role {role!r}, expected one of {ROLES}"
        return None

    def check(self, keyword: object, role: object, decision: object) -> tuple[str, str, str]:
        """The three fields as text, or `ValueError` saying which is wrong."""
        if not isinstance(role, str) or role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        return self.keyword("keyword", keyword), role, self.decision(decision)


def has_verdict(conn: sqlite3.Connection, keyword: str, role: str) -> bool:
    """Whether `keyword_roles` has a verdict (not a refusal) for `keyword` in `role`."""
    found = conn.execute(
        "SELECT 1 FROM keyword_roles WHERE keyword = ? AND role = ? AND error IS NULL",
        (keyword, role),
    ).fetchone()
    return found is not None


def require_role(conn: sqlite3.Connection, keyword: str, role: str) -> None:
    """Raise `NoSuchRole` unless `keyword_roles` has a verdict for `keyword` in `role`."""
    if not has_verdict(conn, keyword, role):
        raise NoSuchRole((keyword, role))


def _decision(
    stored: tuple[str, str] | None, entry: dict[str, str] | None
) -> tuple[str | None, str | None, bool]:
    """The decision, its time, and whether it is still waiting for the fold."""
    decision, decided_at = stored or (None, None)
    if entry is None:
        return decision, decided_at, False
    new = None if entry["decision"] == "cleared" else entry["decision"]
    return new, None if new is None else entry["decided_at"], new != decision


def _sources(rows: list[tuple[str, float | None, str | None]]) -> list[dict[str, object]]:
    """A cell's verdicts: source-stated rows first, then judges, each by source name."""
    ordered = sorted(rows, key=lambda r: (r[1] is not None, r[0]))
    return [{"source": source, "score": score, "model": model} for source, score, model in ordered]


def role_cell(
    conn: sqlite3.Connection, overlay: _Overlay, forms: dict[str, str], keyword: str, role: str
) -> dict[str, object]:
    """One (keyword, role): every source's verdict and the decision, the file's over the table's."""
    rows = conn.execute(
        "SELECT source, score, model FROM keyword_roles "
        "WHERE keyword = ? AND role = ? AND error IS NULL",
        (keyword, role),
    ).fetchall()
    stored = conn.execute(
        "SELECT decision, decided_at FROM keyword_role_decisions WHERE keyword = ? AND role = ?",
        (keyword, role),
    ).fetchone()
    decision, decided_at, pending = _decision(stored, overlay.get((keyword, role)))
    return {
        "keyword": keyword,
        "form": forms.get(keyword, keyword),
        "role": role,
        "sources": _sources(rows),
        "decision": decision,
        "decided_at": decided_at,
        "pending": pending,
    }


def roles_json(
    conn: sqlite3.Connection, overlay: _Overlay, forms: dict[str, str], needle: str = ""
) -> dict[str, object]:
    """The Roles tab's two lists.

    `keywords` is up to `LIST_LIMIT` keywords whose readable or stored form contains
    `needle`, by readable form, each with one cell per role it has a verdict in.
    `decided` is every (keyword, role) a person has ruled on, the file's latest over the
    table's, latest first. `stored` counts every keyword with a verdict, `total` those
    matching `needle`.
    """
    every: list[str] = [
        row[0]
        for row in conn.execute("SELECT DISTINCT keyword FROM keyword_roles WHERE error IS NULL")
    ]
    needle = needle.strip().lower()

    def shown(keyword: str) -> str:
        return forms.get(keyword, keyword).lower()

    matched = sorted(
        (k for k in every if not needle or needle in shown(k) or needle in k.lower()),
        key=lambda k: (shown(k), k),
    )
    page = matched[:LIST_LIMIT]

    ruled = {
        (keyword, role): (decision, decided_at)
        for keyword, role, decision, decided_at in conn.execute(
            "SELECT keyword, role, decision, decided_at FROM keyword_role_decisions"
        )
    }

    verdicts: dict[str, dict[str, list[tuple[str, float | None, str | None]]]] = {
        k: {} for k in page
    }
    if page:
        marks = ",".join("?" * len(page))
        for keyword, role, source, score, model in conn.execute(
            "SELECT keyword, role, source, score, model FROM keyword_roles "
            f"WHERE error IS NULL AND keyword IN ({marks})",
            page,
        ):
            verdicts[keyword].setdefault(role, []).append((source, score, model))

    keywords = []
    for keyword in page:
        cells = {}
        for role in ROLES:
            rows = verdicts[keyword].get(role)
            if not rows:
                continue
            decision, decided_at, pending = _decision(
                ruled.get((keyword, role)), overlay.get((keyword, role))
            )
            cells[role] = {
                "sources": _sources(rows),
                "decision": decision,
                "decided_at": decided_at,
                "pending": pending,
            }
        keywords.append({"keyword": keyword, "form": forms.get(keyword, keyword), "cells": cells})

    # A decision whose verdict is gone (a restored older store) is one the fold ignores and a
    # POST cannot clear, so it is not listed.
    decided = []
    for key in ruled.keys() | {(e["keyword"], e["role"]) for e in overlay.values()}:
        keyword, role = key
        decision, decided_at, pending = _decision(ruled.get(key), overlay.get(key))
        if decision is None or not has_verdict(conn, keyword, role):
            continue
        decided.append(
            {
                "keyword": keyword,
                "form": forms.get(keyword, keyword),
                "role": role,
                "decision": decision,
                "decided_at": decided_at,
                "pending": pending,
            }
        )
    decided.sort(key=lambda d: (str(d["decided_at"]), str(d["keyword"]), str(d["role"])))
    decided.reverse()

    return {
        "roles": list(ROLES),
        "stored": len(every),
        "total": len(matched),
        "limit": LIST_LIMIT,
        "keywords": keywords,
        "decided": decided,
    }
