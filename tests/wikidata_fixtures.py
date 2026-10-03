"""A `WikidataSource` built from the recorded SPARQL response in `fixtures/wikidata/`.

`sparql_batch.json` is query.wikidata.org's answer on 2026-10-03 to
`build_query(["tt1375666", "tt0120815", "tt0000000"])`: Inception, Saving
Private Ryan, and an IMDb id no Wikidata item carries.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plexdb.errors import WikidataError
from plexdb.wikidata_client import Statement, parse_results

FIXTURES = Path(__file__).parent / "fixtures" / "wikidata"


def load(name: str = "sparql_batch.json") -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


@dataclass
class RecordedWikidataSource:
    """Answers each query from the recorded statements, filtered to the ids asked.

    `calls` holds the id list of every query, in order. `fail_calls` holds
    1-indexed query numbers that raise instead of answering.
    """

    statements_recorded: list[Statement] = field(default_factory=lambda: parse_results(load()))
    fail_calls: set[int] = field(default_factory=set)
    calls: list[list[str]] = field(default_factory=list)

    def statements(self, imdb_ids: Sequence[str]) -> list[Statement]:
        self.calls.append(list(imdb_ids))
        if len(self.calls) in self.fail_calls:
            raise WikidataError(f"scripted failure on query {len(self.calls)}")
        wanted = set(imdb_ids)
        return [s for s in self.statements_recorded if s[0] in wanted]
