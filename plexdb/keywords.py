"""Normalizing a keyword surface into the form the store keeps, and remembering
every raw spelling that mapped to it.

One namespace, `keywords`, holds every source's keyword facts (ADR-0016) —
`source` in the primary key tells them apart, not the namespace name. Two
sources can call the same idea `Heists`, `bank-heist`, and `bank heist`; a
consumer asking "does this item carry heist" should get one answer, not three
near-misses. `normalize_keyword` is that answer: lowercase, treat whitespace,
dashes and `_` as word breaks, strip the punctuation at each word's edges, then
fold each plural to its singular (simplemma's English lemma, for a word ending
in "s" only) so `heists`, `heists,` and `heist` land on the same row while
`christmas`, `boxing` and `murderer` stay the words they are.

A writer stores a title's keywords through `write_title_keywords`, which keeps
every raw spelling in `keyword_surfaces` and derives the `enrichment` rows from
them with `normalize_keyword` — so a change to normalization re-derives the
store (`rederive_keywords`) instead of fetching every title again — and records
each spelling's stored form in `keyword_forms`, the map a reader uses to show
a person the spelling a source wrote.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

import simplemma

#: The source-agnostic namespace every keyword source writes to (ADR-0016).
NAMESPACE = "keywords"

#: The `enrichment.key` of an ordinary keyword, and of one its source flags as a
#: spoiler (AniList); a reader that counts a title's keywords reads `KEYWORD_KEY`.
KEYWORD_KEY = "keyword"
SPOILER_KEY = "spoiler_keyword"

#: Every role a `keyword_roles` or `keyword_role_decisions` row may carry
#: (ADR-0019). The v14 migration CHECKs the same set in SQL, and that migration
#: is frozen, so this is the copy writers import; `test_keywords.py` holds the
#: two to each other.
ROLES: tuple[str, ...] = ("tone", "era", "region", "theme", "character_trait")

#: Words ending in "s" that are not plurals of the word simplemma would fold
#: them to: proper nouns (`mars`, `alps`, `wales`, `las` of Las Vegas, `cruces`
#: of Las Cruces), plurals whose meaning differs from the singular (`arms`,
#: `goods`-style `customs`, `woods`, `falls`, `states`), and abbreviations.
NEVER_FOLD: frozenset[str] = frozenset(
    {
        "aids",
        "alps",
        "arms",
        "cruces",
        "customs",
        "falls",
        "interspecies",
        "it's",
        "las",
        "mars",
        "ops",
        "ss",
        "states",
        "vis",
        "wales",
        "woods",
    }
)


def _fold_plural(word: str) -> str:
    """`word`'s singular when it is a plural simplemma knows, else `word`.

    Only a word ending in "s" is looked up, and only a strictly shorter lemma is
    taken: simplemma also maps verb forms to their base (`racing` → `race`,
    `opera` → `opus`), which would merge tags that mean different things.
    """
    if not word.endswith("s") or word in NEVER_FOLD:
        return word
    lemma = simplemma.lemmatize(word, lang="en").lower()
    return lemma if len(lemma) < len(word) else word


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

    Lowercase and turn `’` into `'` (so `women’s` folds like `women's`), then
    whitespace, every dash and `_` break words (so `bank-heist`, `bank‑heist`
    and `bank_heist` split into two words the same way `bank heist` already
    does), each word loses the punctuation at its edges (so `quirky,`,
    `(soccer)` and `st.` become `quirky`, `soccer` and `st`, and a lone `&` or
    `/` drops out) together with any invisible format character (`Cf`, such as
    a zero-width space) and any combining mark on that punctuation or with no
    character before it (so `heists` + U+200B + `,` becomes `heist`, and
    `café.` + U+0301 becomes `café`), and each word left has its plural folded
    (`_fold_plural`). Punctuation and format characters inside a word stay —
    `u.s`, `9/11`, the U+200C zero-width non-joiner in a Persian plural — as
    do `#` and `%` at an edge (`c#`, `100%`), and a symbol such as `+` or `$`
    is not punctuation. A surface with no word left normalizes to `""` —
    callers writing a keyword row are expected to have already dropped those
    before calling this.
    """
    text = surface.lower().replace("’", "'")
    text = "".join(" " if _is_word_break(char) else char for char in text)
    words = [_strip_edge_punctuation(word) for word in text.split()]
    return " ".join(_fold_plural(word) for word in words if word)


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
    self-heals every surface the next time a source writes it, rather than
    freezing whatever the first writer saw.

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


@dataclass(frozen=True)
class RawKeyword:
    """One keyword exactly as a source sent it for one title."""

    surface: str
    #: The source's own rank for the keyword on this title, higher first; NULL
    #: when the source ranks nothing.
    rank: int | None = None
    #: The source flags the keyword as a spoiler (AniList).
    spoiler: bool = False


def _higher(a: int | None, b: int | None) -> int | None:
    """The higher of two ranks, where None means unranked rather than zero."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def write_title_keywords(
    conn: sqlite3.Connection,
    item_id: str,
    source: str,
    raw: Iterable[RawKeyword],
    fetched_at: str,
) -> dict[str, str]:
    """Replace `source`'s keywords on `item_id` with `raw`, and return each kept
    surface's stored form.

    The raw spellings go into `keyword_surfaces`, one row per distinct surface
    (two copies of one surface keep the higher rank, and are a spoiler if
    either is); the `enrichment` rows are then derived from them exactly as
    `rederive_keywords` derives the whole store. A surface that normalizes to
    `""` is kept as a raw fact but derives no row. The caller owns the
    transaction.
    """
    merged: dict[str, RawKeyword] = {}
    for keyword in raw:
        surface = keyword.surface.strip()
        if not surface:
            continue
        held = merged.get(surface)
        merged[surface] = RawKeyword(
            surface,
            keyword.rank if held is None else _higher(held.rank, keyword.rank),
            keyword.spoiler or (held is not None and held.spoiler),
        )
    conn.execute("DELETE FROM keyword_surfaces WHERE item_id = ? AND source = ?", (item_id, source))
    conn.executemany(
        "INSERT INTO keyword_surfaces (item_id, source, surface, rank, spoiler, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(item_id, source, k.surface, k.rank, int(k.spoiler), fetched_at) for k in merged.values()],
    )
    stored = {surface: upsert_keyword_form(conn, surface) for surface in merged}
    conn.execute(
        "DELETE FROM enrichment WHERE item_id = ? AND namespace = ? AND source = ? "
        "AND key IN (?, ?)",
        (item_id, NAMESPACE, source, KEYWORD_KEY, SPOILER_KEY),
    )
    _insert_derived(
        conn,
        _derive(
            (item_id, source, k.surface, k.rank, k.spoiler, fetched_at) for k in merged.values()
        ),
    )
    return {surface: keyword for surface, keyword in stored.items() if keyword}


#: A derived `enrichment` row: item_id, source, key, value, fetched_at, rank.
_Derived = tuple[str, str, str, str, str, int | None]


def _derive(
    surfaces: Iterable[tuple[str, str, str, int | None, bool | int, str]],
    normalize: dict[str, str] | None = None,
) -> list[_Derived]:
    """The `enrichment` rows `keyword_surfaces` rows derive: one per (title,
    source, stored form), keeping the highest rank, the newest `fetched_at`, and
    `SPOILER_KEY` when any surface that folded into it is a spoiler."""
    seen = normalize if normalize is not None else {}
    rows: dict[tuple[str, str, str], tuple[bool, str, int | None]] = {}
    for item_id, source, surface, rank, spoiler, fetched_at in surfaces:
        value = seen.get(surface)
        if value is None:
            value = seen[surface] = normalize_keyword(surface)
        if not value:
            continue
        held = rows.get((item_id, source, value))
        if held is None:
            rows[(item_id, source, value)] = (bool(spoiler), fetched_at, rank)
        else:
            rows[(item_id, source, value)] = (
                held[0] or bool(spoiler),
                max(held[1], fetched_at),
                _higher(held[2], rank),
            )
    return [
        (item_id, source, SPOILER_KEY if spoiler else KEYWORD_KEY, value, fetched_at, rank)
        for (item_id, source, value), (spoiler, fetched_at, rank) in rows.items()
    ]


def _insert_derived(conn: sqlite3.Connection, rows: list[_Derived]) -> None:
    conn.executemany(
        "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at, rank) "
        f"VALUES (?, '{NAMESPACE}', ?, ?, ?, ?, ?)",
        rows,
    )


def rederive_keywords(conn: sqlite3.Connection) -> int:
    """Rebuild every keyword `enrichment` row from `keyword_surfaces` with the
    current `normalize_keyword`, and re-point every `keyword_forms` surface at
    its current stored form. Returns the number of keyword rows written.

    What a migration calls when normalization changes: nothing is fetched. The
    caller owns the transaction.
    """
    seen: dict[str, str] = {}
    conn.execute(
        "DELETE FROM enrichment WHERE namespace = ? AND key IN (?, ?)",
        (NAMESPACE, KEYWORD_KEY, SPOILER_KEY),
    )
    rows = _derive(
        conn.execute(
            "SELECT item_id, source, surface, rank, spoiler, fetched_at FROM keyword_surfaces"
        ),
        seen,
    )
    _insert_derived(conn, rows)
    forms = conn.execute("SELECT surface FROM keyword_forms").fetchall()
    for (surface,) in forms:
        if surface not in seen:
            seen[surface] = normalize_keyword(surface)
    conn.execute("DELETE FROM keyword_forms")
    conn.executemany(
        "INSERT INTO keyword_forms (surface, keyword) VALUES (?, ?)",
        [(surface, seen[surface]) for (surface,) in forms if seen[surface]],
    )
    return len(rows)


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
