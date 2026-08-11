"""An `MDBListSource` over in-memory lists, plus the recorded response bodies.

Not a test module itself (no `test_` prefix, so pytest never collects it) —
shared by `test_mdblist_client.py`, which drives `LiveMDBListClient` through
`httpx.MockTransport` against the recordings in `fixtures/mdblist/`, and
`test_collections.py`, which drives the harvest against the fake below.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plexdb.mdblist_client import MDBListEntry, MDBListList

FIXTURES = Path(__file__).parent / "fixtures" / "mdblist"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@dataclass
class FakeMDBListSource:
    """An `MDBListSource` over in-memory maps — no HTTP.

    `entry_calls` records every list id whose entries were asked for, in the
    order asked, so a test can assert a cached list was never fetched rather
    than only that its rows did not change.
    """

    lists: list[MDBListList] = field(default_factory=list)
    entries_by_list: dict[int, list[MDBListEntry]] = field(default_factory=dict)
    entry_calls: list[int] = field(default_factory=list)

    def top_lists(self) -> list[MDBListList]:
        return list(self.lists)

    def list_entries(self, list_id: int) -> list[MDBListEntry]:
        self.entry_calls.append(list_id)
        return list(self.entries_by_list.get(list_id, []))


def a_list(
    list_id: int,
    *,
    name: str = "A List",
    slug: str = "a-list",
    user_name: str = "someone",
    size: int | None = None,
    likes: int | None = None,
) -> MDBListList:
    return MDBListList(
        list_id=list_id, name=name, slug=slug, user_name=user_name, size=size, likes=likes
    )


def an_entry(media_type: str = "movie", **ids: str) -> MDBListEntry:
    """One list entry carrying whatever ids the test names, e.g.
    `an_entry(imdb="tt0468569")` or `an_entry("show", tvdb="81189")`."""
    return MDBListEntry(
        external_ids=tuple((ns, value) for ns, value in ids.items()), media_type=media_type
    )
