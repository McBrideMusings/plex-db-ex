"""A `PlexSource` built from the recorded fixtures in `fixtures/plex/`.

Not a test module itself (no `test_` prefix, so pytest never collects it) —
shared by `test_walk.py`, which drives `walk_all` directly, and
`test_cli.py`, which drives the same recordings through the `walk` command.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plexdb.plex_client import PLEX_TYPE_EPISODE, PLEX_TYPE_MOVIE, PLEX_TYPE_SHOW, Section

FIXTURES = Path(__file__).parent / "fixtures" / "plex"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


@dataclass
class FakeSource:
    """A `PlexSource` over in-memory records — no HTTP, no fixtures I/O per call."""

    section_list: list[Section]
    records: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict)

    def sections(self) -> list[Section]:
        return list(self.section_list)

    def items(self, section_key: str, type_: int) -> list[dict[str, Any]]:
        # Deep-copied so a test mutating the returned records (to simulate a
        # re-match between two walks) can never leak into another test.
        return copy.deepcopy(self.records.get((section_key, type_), []))


def recorded_source() -> FakeSource:
    """The four real sections, with the recorded movie/show/episode fixtures
    wired to the sections they actually came from (Movies=1, TV Shows=2).
    Concerts (4) and Power Hours (3) are real sections with no recording —
    walking them yields zero titles, same as an empty library section."""
    directory = load("sections.json")["MediaContainer"]["Directory"]
    section_list = [
        Section(key=str(s["key"]), type=s["type"], title=s.get("title", "")) for s in directory
    ]
    records = {
        ("1", PLEX_TYPE_MOVIE): load("section_1_type_1.json")["MediaContainer"]["Metadata"],
        ("2", PLEX_TYPE_SHOW): load("section_2_type_2.json")["MediaContainer"]["Metadata"],
        ("2", PLEX_TYPE_EPISODE): load("section_2_type_4.json")["MediaContainer"]["Metadata"],
    }
    return FakeSource(section_list=section_list, records=records)
