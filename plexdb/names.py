"""A person's display name for a keyword (ADR-0021).

`keyword_names` holds at most one name per stored keyword. Plex DB-EX never writes
`plexdb.db` (ADR-0007, ADR-0017), so a name typed there goes into
`name_decisions.json`, which `plexdb fold-name-decisions` applies at the next sweep;
until then `display_labels` lays the file over the table, so the page shows a name
at once.

A name is for a person to read and nothing else: the judges, embeddings, similarity,
the map's vectors and the merge fold all read stored values and `keyword_forms`
spellings (`keywords.readable_surfaces`), never this.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

from .config import Config
from .decisions import DecisionsFile, FoldStats, decisions_dir
from .errors import NameDecisionsError
from .keywords import is_carried, normalize_keyword, readable_forms
from .merge_review import merge_map

__all__ = [
    "NAME_DECISIONS_FILE",
    "NAME_MAX",
    "NameDecisions",
    "NoSuchKeyword",
    "display_labels",
    "fold_name_decisions",
    "name_decisions_path",
    "names_json",
    "own_labels",
]

NAME_DECISIONS_FILE = "name_decisions.json"

#: The longest display name a person may give.
NAME_MAX = 200

_Overlay = dict[tuple[str, ...], dict[str, str]]


class NoSuchKeyword(LookupError):
    """A name was given to a keyword no title carries (`keywords.is_carried`)."""


class NameDecisions(DecisionsFile):
    """`name_decisions.json`: one entry per keyword, keyed `(surface,)`, one spelling of
    the keyword, with `decision` `named` and the `name`, or `cleared`."""

    KEY: ClassVar[tuple[str, ...]] = ("surface",)
    DECISIONS: ClassVar[tuple[str, ...]] = ("named", "cleared")
    VALUES: ClassVar[tuple[str, ...]] = ("name",)
    ERROR = NameDecisionsError
    NOUN = "name decisions"

    @classmethod
    def key_problem(cls, key: tuple[str, ...]) -> str | None:
        return None if key[0] else "has an empty surface"

    @classmethod
    def values_problem(cls, values: dict[str, str]) -> str | None:
        name = values["name"]
        if not name.strip():
            return "has a blank name"
        return None if len(name) <= NAME_MAX else f"has a name over {NAME_MAX} characters"

    @classmethod
    def resolve(cls, key: tuple[str, ...]) -> tuple[str, ...] | None:
        """The stored keyword; None when the spelling normalizes to nothing."""
        keyword = normalize_keyword(key[0])
        return (keyword,) if keyword else None

    @classmethod
    def spell(
        cls, key: tuple[str, ...], spellings: dict[str, list[str]]
    ) -> Iterator[tuple[str, ...]]:
        return ((surface,) for surface in spellings.get(key[0]) or [key[0]])

    def check(self, keyword: object, name: object) -> tuple[str, str, str]:
        """`(keyword, decision, name)`, or `ValueError` saying which is wrong. A name that
        is empty, blank or null clears the keyword's name."""
        keyword = self.keyword("keyword", keyword)
        if name is None:
            return keyword, "cleared", ""
        if not isinstance(name, str):
            raise ValueError("name must be text or null")
        name = " ".join(name.split())
        if len(name) > NAME_MAX:
            raise ValueError(f"name must be at most {NAME_MAX} characters")
        return keyword, ("named" if name else "cleared"), name


def name_decisions_path(config: Config) -> Path:
    """`name_decisions.json`, beside Plex DB-EX's saved-queries file."""
    return decisions_dir(config) / NAME_DECISIONS_FILE


def fold_name_decisions(conn: sqlite3.Connection, path: Path) -> FoldStats:
    """Apply `path` to `keyword_names`; a missing file is no decisions.

    The file is a list of `{surface, decision, name, decided_at}`, where `decision`
    is `named` (with `name`) or `cleared`. Each entry names the keyword its spelling
    normalizes to now (`NameDecisions.resolve`), so a name survives a migration that
    renames stored values. Entries apply in list order, so the last entry for a
    keyword wins. `named` writes the row; `cleared` deletes it. An entry naming a
    keyword no title carries is counted and ignored, and comes back once the keyword
    does. The whole file is validated before the first row changes, and applied in
    one transaction. It is applied every run, so the file stays the record of the
    latest name per keyword.
    """
    stats = FoldStats()
    entries = NameDecisions.load(path)
    if entries is None:
        return stats
    stats.file_found = True
    stats.entries = len(entries)
    with conn:
        for key, entry in NameDecisions.resolved(entries):
            if key is None or not is_carried(conn, key[0]):
                stats.unmatched += 1
                continue
            stats.matched += 1
            if entry["decision"] == "cleared":
                conn.execute("DELETE FROM keyword_names WHERE keyword = ?", key)
            else:
                conn.execute(
                    "INSERT INTO keyword_names (keyword, name, decided_at) VALUES (?, ?, ?) "
                    "ON CONFLICT (keyword) DO UPDATE SET "
                    "name = excluded.name, decided_at = excluded.decided_at",
                    (*key, entry["name"], entry["decided_at"]),
                )
    return stats


def names_json(conn: sqlite3.Connection, overlay: _Overlay) -> dict[str, dict[str, object]]:
    """Each keyword with a name in the store or in `overlay` (`NameDecisions.latest`):
    the name it shows (None when the file clears it), when it was set, and whether
    the file differs from the store (`pending`, applied at the next sweep)."""
    stored = {
        keyword: (name, decided_at)
        for keyword, name, decided_at in conn.execute(
            "SELECT keyword, name, decided_at FROM keyword_names"
        )
    }
    out: dict[str, dict[str, object]] = {
        keyword: {"name": name, "decided_at": decided_at, "pending": False}
        for keyword, (name, decided_at) in stored.items()
    }
    for (keyword,), entry in overlay.items():
        name = None if entry["decision"] == "cleared" else entry["name"]
        held = stored.get(keyword)
        if name is None and held is None:
            continue
        out[keyword] = {
            "name": name,
            "decided_at": entry["decided_at"],
            "pending": held is None or held[0] != name,
        }
    return out


def _named(conn: sqlite3.Connection, overlay: _Overlay) -> dict[str, dict[str, object]]:
    return {k: v for k, v in names_json(conn, overlay).items() if v["name"] is not None}


def own_labels(conn: sqlite3.Connection, overlay: _Overlay) -> dict[str, str]:
    """For each keyword with a `keyword_forms` row or a name, its own name
    (`names_json`), else its readable spelling (`readable_forms`) — never one borrowed
    from its merge group. The Merges tab labels a pair's two sides with these, since
    both can sit in one group. A keyword absent here is shown as its stored form."""
    labels = readable_forms(conn)
    labels.update({keyword: str(v["name"]) for keyword, v in _named(conn, overlay).items()})
    return labels


def display_labels(conn: sqlite3.Connection, overlay: _Overlay) -> dict[str, str]:
    """For each keyword with a `keyword_forms` row or a name, the text to show a
    person: `own_labels`, except that a merge group whose head has no name shows the
    newest name any of its members has, so a name set on a keyword stays on screen
    when title counts make another member the head. A keyword absent here is shown
    as its stored form.
    """
    labels = own_labels(conn, overlay)
    named = _named(conn, overlay)
    if not named:
        return labels
    newest: dict[str, dict[str, object]] = {}
    for member, head in merge_map(conn).items():
        if member not in named or head in named:
            continue
        held = newest.get(head)
        if held is None or str(named[member]["decided_at"]) > str(held["decided_at"]):
            newest[head] = named[member]
    labels.update({head: str(v["name"]) for head, v in newest.items()})
    return labels
