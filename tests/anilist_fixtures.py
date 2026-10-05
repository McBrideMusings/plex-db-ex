"""An `AniListSource` built from the recorded responses in `fixtures/anilist/`.

`page_batch.json` is graphql.anilist.co's answer on 2026-10-03 to `QUERY` with
ids `[5114, 300, 1225, 999999999]`: Fullmetal Alchemist: Brotherhood, the two
3x3 Eyes OVAs that TMDB lists as one show (tv 62913), and an id AniList has no
entry for. `mapping_excerpt.json` is those three entries from Fribb's
`anime-list-full.json` on the same day, verbatim.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plexdb.anilist_client import Tag, parse_media
from plexdb.errors import AniListError

FIXTURES = Path(__file__).parent / "fixtures" / "anilist"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@dataclass
class RecordedAniListSource:
    """Answers from the recorded page, filtered to the ids asked.

    `calls` holds the id list of every `tags` call, in order. `fail_calls` holds
    1-indexed call numbers that raise instead of answering.
    """

    entries: list[dict[str, Any]] = field(default_factory=lambda: load("mapping_excerpt.json"))
    recorded: dict[int, list[Tag]] = field(
        default_factory=lambda: parse_media(load("page_batch.json"))
    )
    fail_calls: set[int] = field(default_factory=set)
    calls: list[list[int]] = field(default_factory=list)

    def mapping(self) -> list[dict[str, Any]]:
        return self.entries

    def tags(self, anilist_ids: Sequence[int]) -> dict[int, list[Tag]]:
        self.calls.append(list(anilist_ids))
        if len(self.calls) in self.fail_calls:
            raise AniListError(f"scripted failure on call {len(self.calls)}")
        return {a: self.recorded[a] for a in anilist_ids if a in self.recorded}
