"""Reading crowd lists back out of the store: the list table and one list's detail.

These two functions are the one procedure `plexdb lists`, `plexdb list` and the
explorer's Lists tab all call, so what a person sees in a terminal and what the
page shows cannot drift apart.

**Nothing here is stored** (ADR-0012). Owned count, owned share, watched count
and a keyword's over-representation are computed from what the source said and
what the library holds, at the moment of the read. A title walked tomorrow
therefore joins its lists with nothing rewritten (ADR-0027).

**An entry is owned when it resolves to an item.** It resolves through its stored
`(ns, value, kind)` in `external_ids`, and failing that through the membership
`harvest-mdblist` wrote at the same position, because the harvest tries every id
an entry carries while the entry row keeps only the strongest.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

from . import keywords
from .list_decisions import canonical_list_url, is_before, letterboxd_list_parts, list_source

#: Failures in a row after which a list is left alone until a `retry` decision
#: (`crates/plexdb/src/collections.rs` `MAX_FAILURES`, which stops the fetching).
MAX_FAILURES = 3

#: A keyword is reported for a list when at least this many of the list's owned
#: titles carry it …
MIN_KEYWORD_TITLES = 3
#: … and the share of the list's owned titles carrying it is at least this many
#: times the share of the library's titles of that kind carrying it.
MIN_KEYWORD_LIFT = 3.0
#: The most keywords a list's detail names, strongest lift first.
MAX_KEYWORDS = 20

_RESOLVED = """
WITH resolved AS (
    SELECT e.collection_id, e.rank, e.ns, e.value, e.kind, e.title, e.year,
           COALESCE(
               (SELECT x.item_id FROM external_ids x
                 WHERE x.ns = e.ns AND x.value = e.value AND x.kind = e.kind),
               (SELECT m.item_id FROM collection_membership m
                 WHERE m.collection_id = e.collection_id AND m.rank = e.rank)
           ) AS item_id
    FROM collection_entry e
)
"""

#: A movie is watched by a play of itself, a show by a play of any of its episodes.
_WATCHED = (
    "(EXISTS (SELECT 1 FROM plays p WHERE p.item_id = r.item_id) "
    "OR EXISTS (SELECT 1 FROM items ep JOIN plays p ON p.item_id = ep.item_id "
    "WHERE ep.show_item_id = r.item_id))"
)


def _author(url: str | None) -> str | None:
    """The list's owner as the URL names it — `lina` for
    `https://mdblist.com/lists/lina/top-watched` or
    `https://letterboxd.com/lina/list/top` — or `None` for a URL that does not follow
    either shape."""
    if not url:
        return None
    letterboxd = letterboxd_list_parts(url)
    if letterboxd is not None:
        return letterboxd[0]
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "lists":
        return parts[1]
    return None


def _facts(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "collection_id": row["collection_id"],
        "source": row["source"],
        "author": _author(row["url"]),
        "name": row["name"],
        "url": row["url"],
        "provenance": row["provenance"],
        "seed_count": row["seed_count"],
        "entry_count": row["entries"],
        "owned": row["owned"],
        "size": row["size"],
        "watched": row["watched"],
        "likes": row["likes"],
        "fetched_at": row["observed_at"],
    }


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _key(row: dict[str, Any]) -> str:
    """The key a list's decisions and failed fetches are filed under: its canonical URL,
    else its `collection_id`."""
    return (canonical_list_url(row["url"] or "") if row["url"] else None) or str(
        row["collection_id"]
    )


def _annotate(
    conn: sqlite3.Connection, rows: list[dict[str, Any]], *, with_unfetched: bool
) -> list[dict[str, Any]]:
    """Add what a person decided about each list and how its fetches are going.

    `decision` is the last decision folded from `list_decisions.json` (`decision`,
    `decided_at`), or `None`. `failure` is `{attempts, attempted_at, reason, stopped}` for a
    list whose latest fetches failed (`stopped` once it has failed `MAX_FAILURES` times in
    a row and waits for a `retry`), else `None`. `state` is `failed`, `waiting` (added, not
    yet fetched) or `ok`. With `with_unfetched`, a list that was added or failed before it
    was ever stored gets a row of its own, with no `collection_id`.
    """
    decisions: dict[str, sqlite3.Row] = {}
    attempts: dict[str, sqlite3.Row] = {}
    if _has_table(conn, "list_decision"):
        decisions = {row["url"]: row for row in conn.execute("SELECT * FROM list_decision")}
    if _has_table(conn, "list_attempt"):
        attempts = {row["list_key"]: row for row in conn.execute("SELECT * FROM list_attempt")}
    stored: set[str] = set()
    for row in rows:
        key = _key(row)
        stored.add(key)
        decided, failed = decisions.get(key), attempts.get(key)
        row["decision"] = (
            None if decided is None else {k: decided[k] for k in ("decision", "decided_at")}
        )
        row["failure"] = _failure(failed)
        row["state"] = "failed" if failed else "ok"
    if with_unfetched:
        for key in sorted(
            (set(attempts) | {u for u, d in decisions.items() if d["added"]}) - stored
        ):
            decided, failed = decisions.get(key), attempts.get(key)
            if decided is not None and decided["decision"] in ("excluded", "removed"):
                continue
            rows.append(
                _unfetched_row(
                    key,
                    None
                    if decided is None
                    else {k: decided[k] for k in ("decision", "decided_at")},
                    _failure(failed),
                )
            )
    return rows


def _unfetched_row(
    key: str, decision: dict[str, str] | None, failure: dict[str, Any] | None
) -> dict[str, Any]:
    """The table row of a list a person added that the store holds nothing of yet."""
    return {
        "collection_id": None,
        "source": list_source(key) or "mdblist",
        "author": _author(key),
        "name": None,
        "url": key,
        "provenance": "added",
        "seed_count": None,
        "entry_count": 0,
        "owned": 0,
        "size": None,
        "watched": 0,
        "likes": None,
        "fetched_at": None,
        "decision": decision,
        "failure": failure,
        "state": "failed" if failure else "waiting",
    }


def with_pending(
    rows: list[dict[str, Any]], latest: dict[tuple[str, ...], dict[str, str]]
) -> list[dict[str, Any]]:
    """Lay `list_decisions.json` (`ListDecisions.latest`) over the table.

    Each row's `decision` becomes the file's entry when there is one, and `pending` says
    in words what the next sweep will do about it, or `None` when it has nothing left to
    do: "Waiting for sweep" for an added list not yet fetched, a refetch not yet done or a
    retry not yet tried; "Excluded · next sweep removes it"; "Removed · next sweep drops
    it". A list the file adds that the store knows nothing of gets a row of its own.
    """
    known: set[str] = set()
    for row in rows:
        key = _key(row)
        known.add(key)
        entry = latest.get((key,))
        if entry is not None:
            row["decision"] = {"decision": entry["decision"], "decided_at": entry["decided_at"]}
        row["pending"] = _pending(row)
    for (key,), entry in latest.items():
        if key in known or entry["decision"] != "added":
            continue
        row = _unfetched_row(
            key, {"decision": entry["decision"], "decided_at": entry["decided_at"]}, None
        )
        row["pending"] = _pending(row)
        rows.append(row)
    return rows


def _pending(row: dict[str, Any]) -> str | None:
    decided = row["decision"]
    if decided is None:
        return None
    verdict, when = decided["decision"], decided["decided_at"]
    failure = row["failure"]
    if verdict == "excluded":
        return "Excluded · next sweep removes it"
    if verdict == "removed":
        stored = row["collection_id"] is not None or failure is not None
        return "Removed · next sweep drops it" if row["provenance"] == "added" and stored else None
    waiting = False
    if verdict == "added":
        waiting = row["collection_id"] is None
    elif verdict == "refetch":
        waiting = row["fetched_at"] is None or is_before(row["fetched_at"], when)
    elif verdict == "retry":
        waiting = failure is not None and is_before(failure["attempted_at"], when)
    return "Waiting for sweep" if waiting else None


def _failure(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "attempts": row["attempts"],
        "attempted_at": row["attempted_at"],
        "reason": row["reason"],
        "stopped": row["attempts"] >= MAX_FAILURES,
    }


def _table_rows(
    conn: sqlite3.Connection,
    *,
    source: str | None,
    collection_id: str | None,
    with_unfetched: bool = False,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        _RESOLVED + f"SELECT c.collection_id, c.source, c.name, c.url, c.provenance, c.seed_count, "  # noqa: S608
        "c.size, c.likes, c.observed_at, "
        "COUNT(r.ns) AS entries, COUNT(DISTINCT r.item_id) AS owned, "
        f"COUNT(DISTINCT CASE WHEN {_WATCHED} THEN r.item_id END) AS watched "
        "FROM collection c LEFT JOIN resolved r ON r.collection_id = c.collection_id "
        "WHERE (?1 IS NULL OR c.source = ?1) AND (?2 IS NULL OR c.collection_id = ?2) "
        "GROUP BY c.collection_id ORDER BY c.source, c.likes DESC, c.collection_id",
        (source, collection_id),
    ).fetchall()
    return _annotate(conn, [_facts(row) for row in rows], with_unfetched=with_unfetched)


def list_table(conn: sqlite3.Connection, *, source: str | None = None) -> list[dict[str, Any]]:
    """One row per crowd list: where it came from, how it arrived, how much of it
    the library owns and has watched, and what the source said about it.

    `entry_count` is how many entries are stored, `owned` how many distinct library
    titles they resolve to, `size` and `likes` what the source reported, and
    `watched` how many owned titles have at least one play (a show: a play of any
    of its episodes). `source` limits the
    table to one source's lists.

    Each row also says what a person decided about the list (`decision`) and whether its
    fetches are failing (`failure`, `state`); a list added or failing before it was ever
    stored comes last, with no `collection_id` (`_annotate`).
    """
    rows = _table_rows(conn, source=source, collection_id=None, with_unfetched=True)
    return rows if source is None else [row for row in rows if row["source"] == source]


def find_list(conn: sqlite3.Connection, ref: str) -> str | None:
    """The `collection_id` a reference names — an id, or the list's URL with or
    without a trailing slash — or `None` where no list matches."""
    row = conn.execute(
        "SELECT collection_id FROM collection WHERE collection_id = ?1 "
        "OR rtrim(url, '/') = rtrim(?1, '/') LIMIT 1",
        (ref,),
    ).fetchone()
    return None if row is None else str(row[0])


def list_detail(conn: sqlite3.Connection, ref: str) -> dict[str, Any] | None:
    """One list's facts as `list_table` gives them, every entry in list order, and
    the keywords its owned titles carry far more often than the library does.

    `ref` is a `collection_id` or the list's URL; `None` where none matches.

    An owned entry carries its `item_id` and the library's own title and year;
    an unowned one carries the source's. `keywords` compares the list's owned
    titles of its most common type against every library title of that type: a
    keyword is named when at least `MIN_KEYWORD_TITLES` list titles carry it and
    its share of the list is `MIN_KEYWORD_LIFT` times its share of the library.
    """
    collection_id = find_list(conn, ref)
    if collection_id is None:
        return None
    detail = _table_rows(conn, source=None, collection_id=collection_id)[0]
    rows = conn.execute(
        _RESOLVED
        + "SELECT r.rank, r.ns, r.value, r.kind, r.title AS source_title, r.year AS source_year, "
        f"r.item_id, i.title AS item_title, i.year AS item_year, i.type AS item_type, {_WATCHED} "
        "AS watched FROM resolved r LEFT JOIN items i ON i.item_id = r.item_id "
        "WHERE r.collection_id = ? ORDER BY r.rank IS NULL, r.rank, r.ns, r.value",
        (collection_id,),
    ).fetchall()
    detail["entries"] = [
        {
            "rank": row["rank"],
            "owned": row["item_id"] is not None,
            "item_id": row["item_id"],
            "title": row["item_title"] if row["item_id"] is not None else row["source_title"],
            "year": row["item_year"] if row["item_id"] is not None else row["source_year"],
            "ns": row["ns"],
            "value": row["value"],
            "kind": row["kind"],
            "watched": bool(row["watched"]),
        }
        for row in rows
    ]
    return _with_keywords(conn, detail, rows)


def _with_keywords(
    conn: sqlite3.Connection, detail: dict[str, Any], rows: Sequence[sqlite3.Row]
) -> dict[str, Any]:
    owned = {row["item_id"]: row["item_type"] for row in rows if row["item_id"] is not None}
    detail["keywords"] = []
    detail["keyword_kind"] = None
    detail["keyword_titles_in_list"] = 0
    detail["keyword_titles_in_library"] = 0
    if not owned:
        return detail
    kind = sorted(Counter(owned.values()).items(), key=lambda pair: (-pair[1], pair[0]))[0][0]
    in_list_ids = sorted(item_id for item_id, item_type in owned.items() if item_type == kind)
    library_titles = int(
        conn.execute("SELECT COUNT(*) FROM items WHERE type = ?", (kind,)).fetchone()[0]
    )
    detail["keyword_kind"] = kind
    detail["keyword_titles_in_list"] = len(in_list_ids)
    detail["keyword_titles_in_library"] = library_titles

    counts: Counter[str] = Counter()
    for start in range(0, len(in_list_ids), 500):
        chunk = in_list_ids[start : start + 500]
        marks = ", ".join("?" * len(chunk))
        for row in conn.execute(
            "SELECT value, COUNT(DISTINCT item_id) FROM enrichment "  # noqa: S608
            f"WHERE namespace = ? AND key = ? AND item_id IN ({marks}) GROUP BY value",
            (keywords.NAMESPACE, keywords.KEYWORD_KEY, *chunk),
        ):
            counts[row[0]] += row[1]

    found: list[dict[str, Any]] = []
    for keyword, in_list in counts.items():
        if in_list < MIN_KEYWORD_TITLES:
            continue
        in_library = int(
            conn.execute(
                "SELECT COUNT(DISTINCT e.item_id) FROM enrichment e "
                "JOIN items i ON i.item_id = e.item_id "
                "WHERE e.namespace = ? AND e.key = ? AND e.value = ? AND i.type = ?",
                (keywords.NAMESPACE, keywords.KEYWORD_KEY, keyword, kind),
            ).fetchone()[0]
        )
        lift = (in_list / len(in_list_ids)) / (in_library / library_titles)
        if lift >= MIN_KEYWORD_LIFT:
            found.append(
                {"keyword": keyword, "in_list": in_list, "in_library": in_library, "lift": lift}
            )
    found.sort(key=lambda row: (-row["lift"], -row["in_list"], row["keyword"]))
    detail["keywords"] = found[:MAX_KEYWORDS]
    return detail
