"""A person's decisions about crowd lists: add, exclude, refetch, retry, remove.

Plex DB-EX never writes `plexdb.db` (ADR-0007, ADR-0017), so a decision made on its
Lists tab, or by `plexdb list-decide`, goes into `list_decisions.json` beside the other
decisions files (`decisions.py`), and `plexdb fold-list-decisions` folds it into
`list_decision` at the next sweep. The list harvest reads that table
(`collections.refresh_mdblist`, `letterboxd_lists.refresh_letterboxd_lists`); this module
only records and folds.

An entry names a list by URL. The URL is canonical (`canonical_list_url`), so
`https://www.mdblist.com/lists/Lina/Top-Watched/` and the stored
`https://mdblist.com/lists/lina/top-watched` are one key, and the last entry per key wins.
A Letterboxd list is keyed `https://letterboxd.com/<user>/list/<slug>`.

`list_decision.added` outlives the entry that set it. The file keeps one entry per URL, so a
`refetch` or `retry` on an added list replaces its `added` entry; the table keeps the fact
that a person added the list until an `excluded` or `removed` decision ends it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote, urlparse

from .config import Config
from .decisions import DecisionsFile, FoldStats, decisions_dir
from .errors import ListDecisionsError

__all__ = [
    "LIST_DECISIONS_FILE",
    "ListDecisions",
    "canonical_list_url",
    "fold_list_decisions",
    "is_before",
    "letterboxd_list_parts",
    "list_decisions_path",
    "list_source",
    "list_url_parts",
]

LIST_DECISIONS_FILE = "list_decisions.json"

MDBLIST = "mdblist"
LETTERBOXD = "letterboxd"

_MDBLIST_HOSTS = frozenset({"mdblist.com", "www.mdblist.com"})
_LETTERBOXD_HOSTS = frozenset({"letterboxd.com", "www.letterboxd.com"})


def _path_parts(raw: str, hosts: frozenset[str]) -> list[str] | None:
    try:
        url = urlparse(raw.strip())
    except ValueError:
        return None
    if url.scheme not in ("http", "https") or (url.hostname or "") not in hosts:
        return None
    return [unquote(part) for part in url.path.split("/") if part]


def list_url_parts(raw: str) -> tuple[str, str] | None:
    """`(user, slug)`, lowercased, of an MDBList list URL
    (`https://mdblist.com/lists/<user>/<slug>`), or None for any other text."""
    parts = _path_parts(raw, _MDBLIST_HOSTS)
    if (
        parts is None
        or len(parts) != 3
        or parts[0].lower() != "lists"
        or not parts[1]
        or not parts[2]
    ):
        return None
    return parts[1].lower(), parts[2].lower()


def letterboxd_list_parts(raw: str) -> tuple[str, str] | None:
    """`(user, slug)`, lowercased, of a Letterboxd list URL
    (`https://letterboxd.com/<user>/list/<slug>/`, a `/page/N/` suffix allowed), or None
    for any other text."""
    parts = _path_parts(raw, _LETTERBOXD_HOSTS)
    if (
        parts is None
        or len(parts) < 3
        or parts[1].lower() != "list"
        or not parts[0]
        or not parts[2]
    ):
        return None
    rest = parts[3:]
    if rest and not (
        len(rest) == 2 and rest[0].lower() == "page" and rest[1].isascii() and rest[1].isdigit()
    ):
        return None
    return parts[0].lower(), parts[2].lower()


def canonical_list_url(raw: str) -> str | None:
    """The one spelling of a list URL a store keys on — MDBList's or Letterboxd's — or
    None for text that is neither."""
    parts = list_url_parts(raw)
    if parts is not None:
        return f"https://mdblist.com/lists/{parts[0]}/{parts[1]}"
    parts = letterboxd_list_parts(raw)
    if parts is not None:
        return f"https://letterboxd.com/{parts[0]}/list/{parts[1]}"
    return None


def list_source(raw: str) -> str | None:
    """`collection.source` of the list a URL names — `mdblist` or `letterboxd` — or None
    for text that is neither."""
    if list_url_parts(raw) is not None:
        return MDBLIST
    if letterboxd_list_parts(raw) is not None:
        return LETTERBOXD
    return None


def is_before(earlier: str, later: str) -> bool:
    """Whether the ISO 8601 time `earlier` is before `later`; a time that does not parse
    is not before anything. A time with no offset is UTC."""
    try:
        first, second = datetime.fromisoformat(earlier), datetime.fromisoformat(later)
    except ValueError:
        return False
    first = first if first.tzinfo else first.replace(tzinfo=UTC)
    second = second if second.tzinfo else second.replace(tzinfo=UTC)
    return first < second


class ListDecisions(DecisionsFile):
    """`list_decisions.json`: one entry per list, keyed `(url,)`, with `decision` one of
    `added`, `excluded`, `refetch`, `retry` or `removed`."""

    KEY: ClassVar[tuple[str, ...]] = ("url",)
    DECISIONS: ClassVar[tuple[str, ...]] = ("added", "excluded", "refetch", "retry", "removed")
    ERROR = ListDecisionsError
    NOUN = "list decisions"
    URL_MAX = 300

    @classmethod
    def key_problem(cls, key: tuple[str, ...]) -> str | None:
        return (
            None
            if canonical_list_url(key[0])
            else "has a url that is not an MDBList or Letterboxd list"
        )

    @classmethod
    def resolve(cls, key: tuple[str, ...]) -> tuple[str, ...] | None:
        url = canonical_list_url(key[0])
        return None if url is None else (url,)

    @classmethod
    def spell(
        cls, key: tuple[str, ...], spellings: dict[str, list[str]]
    ) -> Iterator[tuple[str, ...]]:
        return iter([key])

    def check(self, url: object, decision: object) -> tuple[str, str]:
        """`(canonical url, decision)`, or `ValueError` saying which is wrong."""
        if not isinstance(url, str) or not url or len(url) > self.URL_MAX:
            raise ValueError(f"url must be text of 1 to {self.URL_MAX} characters")
        canonical = canonical_list_url(url)
        if canonical is None:
            raise ValueError(
                "url must be an MDBList list, like https://mdblist.com/lists/<user>/<list>, "
                "or a Letterboxd list, like https://letterboxd.com/<user>/list/<list>/"
            )
        return canonical, self.decision(decision)


def list_decisions_path(config: Config) -> Path:
    """`list_decisions.json`, beside Plex DB-EX's saved-queries file."""
    return decisions_dir(config) / LIST_DECISIONS_FILE


def fold_list_decisions(conn: sqlite3.Connection, path: Path) -> FoldStats:
    """Apply `path` to `list_decision`; a missing file is no decisions.

    The file is a list of `{url, decision, decided_at}`. Entries apply in list order, so
    the last entry per canonical URL wins. `added` sets the row's `added` flag; `excluded`
    and `removed` clear it; `refetch` and `retry` keep what the row said. The whole file is
    validated before the first row changes and applied in one transaction, every run, so
    the file stays the record of the latest decision per list. Whether a decision has had
    its effect is the harvest's to work out from `decided_at`.
    """
    stats = FoldStats()
    entries = ListDecisions.load(path)
    if entries is None:
        return stats
    stats.file_found = True
    stats.entries = len(entries)
    with conn:
        for key, entry in ListDecisions.resolved(entries):
            if key is None:
                stats.unmatched += 1
                continue
            stats.matched += 1
            decision = entry["decision"]
            held = conn.execute("SELECT added FROM list_decision WHERE url = ?", key).fetchone()
            added = {"added": 1, "excluded": 0, "removed": 0}.get(
                decision, 0 if held is None else held[0]
            )
            conn.execute(
                "INSERT INTO list_decision (url, decision, decided_at, added) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (url) DO UPDATE SET decision = excluded.decision, "
                "decided_at = excluded.decided_at, added = excluded.added",
                (*key, decision, entry["decided_at"], added),
            )
    return stats


class UnknownList(LookupError):
    """A refetch, retry or removal named a list the store has no record of."""


def known_list_urls(conn: sqlite3.Connection) -> set[str]:
    """The canonical URL of every list the store holds, has decided or has failed to fetch."""
    urls: set[str] = set()
    queries = (
        "SELECT url FROM collection WHERE url IS NOT NULL",
        "SELECT url FROM list_decision",
        "SELECT list_key FROM list_attempt",
    )
    for query in queries:
        for (raw,) in conn.execute(query):
            canonical = canonical_list_url(raw)
            if canonical is not None:
                urls.add(canonical)
    return urls


def decide(
    conn: sqlite3.Connection, decisions: ListDecisions, url: object, decision: object
) -> dict[str, str]:
    """Append one decision to the file and return its entry.

    Anyone may add or exclude any MDBList or Letterboxd list URL. A refetch, retry or removal
    must name a list the store (or the file) already knows, so a caller cannot grow the file with
    made-up lists. `ValueError` for a bad field, `UnknownList` for an unknown list.
    """
    canonical, verdict = decisions.check(url, decision)
    if verdict not in ("added", "excluded") and (
        canonical not in known_list_urls(conn) and (canonical,) not in decisions.latest()
    ):
        raise UnknownList(canonical)
    pending = decisions.latest().get((canonical,))
    if (
        verdict in ("refetch", "retry")
        and pending is not None
        and pending["decision"] == "added"
        and not conn.execute(
            "SELECT 1 FROM list_decision WHERE url = ? AND added = 1", (canonical,)
        ).fetchone()
    ):
        raise ValueError(
            f"{canonical} was added but no sweep has folded the add yet; "
            f"a {verdict} now would replace it"
        )
    return decisions.record((canonical,), verdict, {})
