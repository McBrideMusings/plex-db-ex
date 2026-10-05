"""An `MDBListSource` over in-memory lists, plus the recorded response bodies.

Not a test module itself (no `test_` prefix, so pytest never collects it) —
shared by `test_mdblist_client.py`, which drives `LiveMDBListClient` through
`httpx.MockTransport` against the recordings in `fixtures/mdblist/`, and
`test_collections.py`, which drives the harvest against the fake below, and
`test_enrich_mdblist_ratings.py`, which drives the ratings sweep against the
batch recordings.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from plexdb.mdblist_client import (
    LiveMDBListClient,
    MDBListEntry,
    MDBListList,
    MDBListTitleRatings,
)

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


@dataclass
class RecordedRatingsSource:
    """An `MDBListRatingsSource` that runs the real `LiveMDBListClient` over an
    `httpx.MockTransport` answering from the two batch recordings.

    Each POST answers with the recorded titles whose IMDb id it asked for, so
    an id outside the recordings is absent exactly as the live service leaves
    an unknown id out. `calls` is every request's `(media_type, ids)`;
    `fail_calls` holds the 1-based request numbers that answer 503.
    """

    fail_calls: set[int] = field(default_factory=set)
    calls: list[tuple[str, list[str]]] = field(default_factory=list)

    def ratings(self, media_type: str, imdb_ids: Sequence[str]) -> list[MDBListTitleRatings]:
        recorded = load("batch_imdb_movie.json") + load("batch_imdb_show.json")

        def handler(request: httpx.Request) -> httpx.Response:
            asked = json.loads(request.content)["ids"]
            self.calls.append((media_type, asked))
            if len(self.calls) in self.fail_calls:
                return httpx.Response(503)
            return httpx.Response(
                200,
                json=[t for t in recorded if t["ids"]["imdb"] in asked and t["type"] == media_type],
            )

        client = LiveMDBListClient(
            "test-key",
            http=httpx.Client(
                transport=httpx.MockTransport(handler), headers={"User-Agent": "plexdb"}
            ),
        )
        return client.ratings(media_type, imdb_ids)


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
