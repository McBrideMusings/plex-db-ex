"""A `PlexSource` and a `HistorySource` built from the recorded fixtures in
`fixtures/plex/`.

Not a test module itself (no `test_` prefix, so pytest never collects it) —
shared by `test_walk.py`, which drives `walk_all` directly; `test_plays.py`,
which drives `ingest_plays` directly; and `test_cli.py`, which drives the
same recordings through the `walk` and `ingest-plays` commands.
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


@dataclass
class FakeHistorySource:
    """A `HistorySource` over in-memory events and devices — no HTTP.

    `since_viewed_at` is honoured the same way `LivePlexClient.history` would
    apply Plex's `viewedAt>` filter server-side, so a test exercising
    incremental fetch doesn't need a live client to see the effect —
    including that filter's measured quirk: **inclusive** despite its name
    (confirmed live: filtering on the exact newest event's own `viewedAt`
    still returns that event). `>=`, not `>`, is what reproduces that.
    """

    history_events: list[dict[str, Any]]
    device_list: list[dict[str, Any]]

    def history(self, *, since_viewed_at: int | None = None) -> list[dict[str, Any]]:
        events = self.history_events
        if since_viewed_at is not None:
            events = [e for e in events if e.get("viewedAt", 0) >= since_viewed_at]
        return copy.deepcopy(events)

    def devices(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.device_list)


def recorded_history_source() -> FakeHistorySource:
    """Four recorded-shape history events and the three devices they
    reference, plus deliberate gaps: one event's rating key (999999) is
    absent from `recorded_source()`'s items, and one event's device id (999)
    is absent from the device list — covering the two "counted, not dropped"
    paths `ingest_plays` must exercise."""
    events = load("history.json")["MediaContainer"]["Metadata"]
    devices = load("devices.json")["MediaContainer"]["Device"]
    return FakeHistorySource(history_events=events, device_list=devices)
