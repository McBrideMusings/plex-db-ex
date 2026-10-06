"""Normalizing a keyword surface into the form the store keeps, and remembering
every raw spelling that mapped to it.

One namespace, `keywords`, holds every source's keyword facts (ADR-0016) —
`source` in the primary key tells them apart, not the namespace name. Two
sources can call the same idea `Heists`, `bank-heist`, and `bank heist`; a
consumer asking "does this item carry heist" should get one answer, not three
near-misses. `normalize_keyword` is that answer: lowercase, treat whitespace,
dashes and `_` as word breaks, strip the punctuation at each word's edges, then
stem each word with the Snowball English algorithm (`snowballstemmer`) so
`heists`, `heists,` and `heist` land on the same row.

Every writer that stores a keyword calls `upsert_keyword_form` rather than
`normalize_keyword` alone, so `keyword_forms(surface, keyword)` always has an
entry mapping the raw spelling it saw back to the stored form — the only way a
reader can show a human "bank heist" instead of the stemmed `bank heist` (or,
after stemming a plural, a form that is not even a real word on its own).
"""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass, field

import snowballstemmer

#: The source-agnostic namespace every keyword source writes to (ADR-0016).
NAMESPACE = "keywords"

#: Every role a `keyword_roles` or `keyword_role_decisions` row may carry
#: (ADR-0019). The v14 migration CHECKs the same set in SQL, and that migration
#: is frozen, so this is the copy writers import; `test_keywords.py` holds the
#: two to each other.
ROLES: tuple[str, ...] = ("tone", "era", "region", "theme", "character_trait")

_stemmer = snowballstemmer.stemmer("english")


#: Punctuation that is part of the word it ends or starts — `c#`, `100%` — and
#: so is never stripped from a word's edge.
_KEPT_AT_EDGE = frozenset("#%")


def _is_punctuation(char: str) -> bool:
    return char not in _KEPT_AT_EDGE and unicodedata.category(char).startswith("P")


def _is_word_break(char: str) -> bool:
    # Pd is every dash (`-`, the non-breaking `‑`, `–`, `—`); Pc is `_` and the
    # other connectors (`‿`, the fullwidth `＿`).
    return char.isspace() or unicodedata.category(char) in ("Pd", "Pc")


def normalize_keyword(surface: str) -> str:
    """The stored form of one raw keyword spelling.

    Lowercase and turn `’` into `'` (the stemmer strips a possessive `'s` only
    in the straight form), then whitespace, every dash and `_` break words (so
    `bank-heist`, `bank‑heist` and `bank_heist` split into two words the same
    way `bank heist` already does), each word loses the punctuation at its
    edges (so `quirky,`, `(soccer)` and `st.` stem like `quirky`, `soccer` and
    `st`, and a lone `&` or `/` drops out) together with any invisible format
    character (`Cf`, such as a zero-width space) and any combining mark on that
    punctuation or with no character before it (so `heists` + U+200B + `,`
    stems like `heists`, and `café.` + U+0301 like `café`), and each word left
    is stemmed. Punctuation and format characters inside a word stay — `u.s`,
    `9/11`, `women's`, the U+200C zero-width non-joiner in a Persian plural —
    as do `#` and `%` at an edge (`c#`, `100%`), and a symbol such as `+` or
    `$` is not punctuation. A surface with no word left normalizes to `""` —
    callers writing a keyword row are expected to have already dropped those
    before calling this.
    """
    text = surface.lower().replace("’", "'")
    text = "".join(" " if _is_word_break(char) else char for char in text)
    words = [_strip_edge_punctuation(word) for word in text.split()]
    return " ".join(_stemmer.stemWords([word for word in words if word]))


def _clusters(word: str) -> list[str]:
    """`word` split into characters, each carrying the combining marks (`M*`)
    that follow it; a mark with nothing before it is a cluster of its own."""
    clusters: list[str] = []
    for char in word:
        if clusters and unicodedata.category(char).startswith("M"):
            clusters[-1] += char
        else:
            clusters.append(char)
    return clusters


def _is_edge_debris(cluster: str) -> bool:
    category = unicodedata.category(cluster[0])
    return _is_punctuation(cluster[0]) or category == "Cf" or category.startswith("M")


def _strip_edge_punctuation(word: str) -> str:
    clusters = _clusters(word)
    start, end = 0, len(clusters)
    while start < end and _is_edge_debris(clusters[start]):
        start += 1
    while end > start and _is_edge_debris(clusters[end - 1]):
        end -= 1
    return "".join(clusters[start:end])


def upsert_keyword_form(conn: sqlite3.Connection, surface: str) -> str:
    """Normalize `surface`, record the mapping in `keyword_forms`, and return
    the stored form to write into `enrichment`.

    `ON CONFLICT ... DO UPDATE` rather than `INSERT OR IGNORE`: the same raw
    spelling always normalizes to the same stored form, so the update is a
    no-op in practice, but it means a change to `normalize_keyword` itself
    (a stemmer version bump, say) self-heals every surface the next time this
    source writes it, rather than freezing whatever the first writer saw.

    A surface that normalizes to `""` (`&`, `...`) is no keyword: nothing is
    recorded, and the caller drops the `""` it gets back.
    """
    keyword = normalize_keyword(surface)
    if not keyword:
        return keyword
    conn.execute(
        "INSERT INTO keyword_forms (surface, keyword) VALUES (?, ?) "
        "ON CONFLICT(surface) DO UPDATE SET keyword = excluded.keyword",
        (surface, keyword),
    )
    return keyword


def readable_surfaces(conn: sqlite3.Connection) -> dict[str, str]:
    """Each stored keyword and the text a person or a judge reads for it: its
    shortest surface form, shortest first then alphabetical so the choice is
    stable. A keyword no `keyword_forms` row maps back to is its own text."""
    chosen: dict[str, str] = {}
    for surface, keyword in conn.execute("SELECT surface, keyword FROM keyword_forms"):
        best = chosen.get(keyword)
        if best is None or (len(surface), surface) < (len(best), best):
            chosen[keyword] = surface
    return {
        row[0]: chosen.get(row[0], row[0])
        for row in conn.execute(
            "SELECT DISTINCT value FROM enrichment WHERE namespace = ? AND key = 'keyword'",
            (NAMESPACE,),
        )
    }


def state_role(
    conn: sqlite3.Connection, keyword: str, role: str, source: str, stated_at: str
) -> None:
    """Record that `source` itself states `keyword` plays `role`.

    A source-stated role carries no score, model or error — those belong to a
    judge's row — so all three stay NULL. `keyword` is the stored form, as
    `upsert_keyword_form` returned it. Stating a role again only moves
    `stated_at`: the row is per keyword, not per title, so every title that
    names the keyword under the role-bearing property restates the same row.
    """
    if role not in ROLES:
        raise ValueError(f"unknown keyword role {role!r}; roles are {', '.join(ROLES)}")
    conn.execute(
        "INSERT INTO keyword_roles (keyword, role, source, stated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(keyword, role, source) DO UPDATE SET stated_at = excluded.stated_at",
        (keyword, role, source, stated_at),
    )


#: Every keyword value some title carries, as a subquery. AniList stores a tag it
#: flags as a spoiler under `spoiler_keyword`, and still states a role for it.
_CARRIED = (
    f"SELECT value FROM enrichment WHERE namespace = '{NAMESPACE}' "
    "AND key IN ('keyword', 'spoiler_keyword')"
)


@dataclass
class PruneStats:
    #: Distinct stored keyword values some title carries.
    keywords_stored: int = 0
    pairs_deleted: int = 0
    roles_deleted: int = 0
    decisions_deleted: int = 0
    #: The keyword values whose rows were deleted, sorted.
    keywords_pruned: list[str] = field(default_factory=list)


def prune_keyword_verdicts(conn: sqlite3.Connection) -> PruneStats:
    """Delete every `keyword_pairs`, `keyword_roles` and `keyword_role_decisions`
    row keyed on a keyword value no `enrichment` row carries.

    A value leaves `enrichment` when a source stops listing it or a change to
    `normalize_keyword` rewrites it on a title's next fetch; the rows keyed on
    it then describe nothing. Jev's scores for a value that returns are asked
    for again. A person's decision goes with its rows: the decisions file it
    came from keeps it, and the fold restores it once the value, or the pair,
    has a verdict again. One transaction.
    """
    stats = PruneStats()
    with conn:
        stats.keywords_stored = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT DISTINCT * FROM ({_CARRIED}))"
        ).fetchone()[0]
        stats.keywords_pruned = [
            row[0]
            for row in conn.execute(
                "SELECT keyword_a FROM keyword_pairs UNION SELECT keyword_b FROM keyword_pairs "
                "UNION SELECT keyword FROM keyword_roles "
                "UNION SELECT keyword FROM keyword_role_decisions "
                f"EXCEPT {_CARRIED} ORDER BY 1"
            )
        ]
        stats.pairs_deleted = conn.execute(
            f"DELETE FROM keyword_pairs "
            f"WHERE keyword_a NOT IN ({_CARRIED}) OR keyword_b NOT IN ({_CARRIED})"
        ).rowcount
        stats.roles_deleted = conn.execute(
            f"DELETE FROM keyword_roles WHERE keyword NOT IN ({_CARRIED})"
        ).rowcount
        stats.decisions_deleted = conn.execute(
            f"DELETE FROM keyword_role_decisions WHERE keyword NOT IN ({_CARRIED})"
        ).rowcount
    return stats
