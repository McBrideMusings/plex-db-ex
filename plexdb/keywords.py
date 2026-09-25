"""Normalizing a keyword surface into the form the store keeps, and remembering
every raw spelling that mapped to it.

One namespace, `keywords`, holds every source's keyword facts (ADR-0016) —
`source` in the primary key tells them apart, not the namespace name. Two
sources can call the same idea `Heists`, `bank-heist`, and `bank heist`; a
consumer asking "does this item carry heist" should get one answer, not three
near-misses. `normalize_keyword` is that answer: lowercase, trim, collapse
whitespace, treat `-` and `_` as word breaks, then stem each word with the
Snowball English algorithm (`snowballstemmer`) so `heists` and `heist` land on
the same row.

Every writer that stores a keyword calls `upsert_keyword_form` rather than
`normalize_keyword` alone, so `keyword_forms(surface, keyword)` always has an
entry mapping the raw spelling it saw back to the stored form — the only way a
reader can show a human "bank heist" instead of the stemmed `bank heist` (or,
after stemming a plural, a form that is not even a real word on its own).
"""

from __future__ import annotations

import re
import sqlite3

import snowballstemmer

#: The source-agnostic namespace every keyword source writes to (ADR-0016).
NAMESPACE = "keywords"

_stemmer = snowballstemmer.stemmer("english")
_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_keyword(surface: str) -> str:
    """The stored form of one raw keyword spelling.

    Lowercase and trim, then `-` and `_` become spaces (so `bank-heist` and
    `bank_heist` split into two words the same way `bank heist` already does),
    runs of whitespace collapse to one space, and each resulting word is
    stemmed. An empty or whitespace-only surface normalizes to `""` — callers
    writing a keyword row are expected to have already dropped those before
    calling this.
    """
    text = surface.strip().lower().replace("-", " ").replace("_", " ")
    text = _WHITESPACE_RUN.sub(" ", text).strip()
    if not text:
        return ""
    return " ".join(_stemmer.stemWords(text.split(" ")))


def upsert_keyword_form(conn: sqlite3.Connection, surface: str) -> str:
    """Normalize `surface`, record the mapping in `keyword_forms`, and return
    the stored form to write into `enrichment`.

    `ON CONFLICT ... DO UPDATE` rather than `INSERT OR IGNORE`: the same raw
    spelling always normalizes to the same stored form, so the update is a
    no-op in practice, but it means a change to `normalize_keyword` itself
    (a stemmer version bump, say) self-heals every surface the next time this
    source writes it, rather than freezing whatever the first writer saw.
    """
    keyword = normalize_keyword(surface)
    conn.execute(
        "INSERT INTO keyword_forms (surface, keyword) VALUES (?, ?) "
        "ON CONFLICT(surface) DO UPDATE SET keyword = excluded.keyword",
        (surface, keyword),
    )
    return keyword
