"""Write `tests/fixtures/keyword_normalization.json`: Python's `normalize_keyword`
for every raw keyword spelling a store holds and every English dictionary word
ending in "s", plus hand-picked edge cases.

The Rust normalizer (`crates/plexdb-keywords`) must return exactly these values;
its test reads the file. The file is the published specification of the rule
(CLAUDE.md), so regenerate it from a freshly pulled store whenever
`plexdb/keywords.py` or the pinned simplemma changes, and commit it with the code.

    uv run python tools/golden_keywords.py [--store data/plexdb.db]
"""

from __future__ import annotations

import argparse
import json
import lzma
import sqlite3
from pathlib import Path

import simplemma
from simplemma.strategies.dictionaries import frontcode

from plexdb.keywords import normalize_keyword

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "tests" / "fixtures" / "keyword_normalization.json"

#: Spellings that exercise one rule each, whatever the store holds.
EDGE_CASES = [
    "",
    " ",
    "&",
    "...",
    "Heists",
    "heists,",
    "bank-heist",
    "bank‑heist",
    "bank_heist",
    "bank heist",
    "women’s",
    "women's",
    "Christmas",
    "boxing",
    "murderer",
    "c#",
    "100%",
    "(soccer)",
    "st.",
    "u.s",
    "9/11",
    "heists​,",
    "café.́",
    "́heists",
    "Mars",
    "Las Cruces",
    "dogs'",
    "don't",
    "can't",
    "الأفلام",
    "ÉCOLES",
    "ǅs",
    "ßs",
]


def dictionary_words() -> list[str]:
    path = Path(simplemma.__file__).parent / "strategies" / "dictionaries" / "data" / "en.plzma"
    with lzma.open(path, "rb") as handle:
        entries = frontcode._decode_stream(handle.read())
    return sorted(key.decode() for key in entries if key.endswith(b"s"))


def store_spellings(store: Path) -> set[str]:
    spellings: set[str] = set()
    with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as conn:
        for sql in (
            "SELECT surface FROM keyword_forms",
            "SELECT DISTINCT surface FROM keyword_surfaces",
        ):
            spellings.update(row[0] for row in conn.execute(sql))
    return spellings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store", type=Path, default=REPO / "data" / "plexdb.db")
    args = parser.parse_args()
    surfaces = set(EDGE_CASES) | set(dictionary_words()) | store_spellings(args.store)
    cases = [[surface, normalize_keyword(surface)] for surface in sorted(surfaces)]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as handle:
        handle.write('{\n  "simplemma": "' + simplemma_version() + '",\n  "cases": [\n')
        handle.write(",\n".join("    " + json.dumps(case, ensure_ascii=False) for case in cases))
        handle.write("\n  ]\n}\n")
    print(f"wrote {len(cases):,} case(s) to {OUT}")
    return 0


def simplemma_version() -> str:
    from importlib.metadata import version

    return version("simplemma")


if __name__ == "__main__":
    raise SystemExit(main())
