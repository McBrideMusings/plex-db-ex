"""`media_walk` records parts and streams from a recorded `/library/metadata` response,
and a second run over an unchanged `updatedAt` makes no request."""

from __future__ import annotations

import copy
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from plex_fixtures import FakeSource, load, recorded_source

from plexdb.media import media_walk
from plexdb.store import init, open_store
from plexdb.walk import walk_all

RATING_KEY = "13714"
ROOTS = ["/media"]


@dataclass
class MediaSource(FakeSource):
    """A `FakeSource` that also answers `metadata`, counting each request."""

    full: dict[str, dict[str, Any]] = field(default_factory=dict)
    requested: list[str] = field(default_factory=list)

    def metadata(self, rating_key: str) -> dict[str, Any] | None:
        self.requested.append(rating_key)
        record = copy.deepcopy(self.full[RATING_KEY])
        if rating_key != RATING_KEY:
            # Every other title in the recording is given the same single part under a
            # part id of its own, so its first run records something to compare against.
            record["Media"][0]["Part"][0]["id"] = int(rating_key)
        return record


def _source(updated_at: int = 1760000000) -> MediaSource:
    base = recorded_source()
    source = MediaSource(section_list=base.section_list, records=base.records)
    for records in source.records.values():
        for record in records:
            record["updatedAt"] = updated_at
    source.full[RATING_KEY] = load("metadata_13714.json")["MediaContainer"]["Metadata"][0]
    return source


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    store = tmp_path / "plexdb.db"
    init(store)
    with open_store(store) as connection:
        walk_all(connection, _source(), ROOTS)
        yield connection


def test_the_recorded_response_writes_the_part_and_its_streams(conn: sqlite3.Connection) -> None:
    source = _source()
    stats = media_walk(conn, source)

    assert RATING_KEY in source.requested
    assert stats.missing == 0
    part = conn.execute("SELECT * FROM media_part WHERE part_id = 31001").fetchone()
    assert part["rating_key"] == RATING_KEY
    assert part["item_id"] == "imdb:tt0096734"
    assert part["size"] == 7021345678
    assert (part["container"], part["video_codec"], part["audio_codec"]) == ("mkv", "h264", "dts")
    assert (part["height"], part["bitrate"], part["duration_ms"]) == (1080, 9200, 6100386)
    assert part["plex_updated_at"] == 1760000000
    streams = conn.execute(
        "SELECT idx, kind, codec, lang, forced, title FROM media_stream "
        "WHERE part_id = 31001 ORDER BY idx"
    ).fetchall()
    assert [tuple(r) for r in streams] == [
        (0, "video", "h264", None, 0, None),
        (1, "audio", "dts", "eng", 0, "English (DTS 5.1)"),
        (2, "audio", "ac3", "fra", 0, "French (AC3 5.1)"),
        (3, "subtitle", "srt", "eng", 1, "English (SRT Forced)"),
        (4, "subtitle", "srt", "spa", 0, "Spanish (SRT)"),
    ]
    assert stats.parts_written == stats.titles_listed


def test_a_second_run_with_an_unchanged_updated_at_fetches_nothing(
    conn: sqlite3.Connection,
) -> None:
    media_walk(conn, _source())

    again = _source()
    stats = media_walk(conn, again)

    assert again.requested == []
    assert stats.fetched == 0
    assert stats.unchanged == stats.titles_listed > 1


def test_a_changed_updated_at_replaces_the_streams_and_drops_vanished_parts(
    conn: sqlite3.Connection,
) -> None:
    media_walk(conn, _source())

    changed = _source(updated_at=1770000000)
    part = changed.full[RATING_KEY]["Media"][0]["Part"][0]
    part["Stream"] = part["Stream"][:2]
    part["id"] = 31002
    media_walk(conn, changed)

    rows = conn.execute("SELECT part_id FROM media_part WHERE rating_key = ?", (RATING_KEY,))
    assert [r[0] for r in rows] == [31002]
    streams = conn.execute("SELECT COUNT(*) FROM media_stream WHERE part_id IN (31001, 31002)")
    assert streams.fetchone()[0] == 2
    assert {r[0] for r in conn.execute("SELECT plex_updated_at FROM media_part")} == {1770000000}


def test_a_title_whose_listing_names_no_file_loses_its_parts_without_a_request(
    conn: sqlite3.Connection,
) -> None:
    media_walk(conn, _source())

    emptied = _source(updated_at=1770000000)
    for record in emptied.records[("1", 1)]:
        record["Media"] = []
    stats = media_walk(conn, emptied)

    assert stats.emptied == len(emptied.records[("1", 1)])
    assert RATING_KEY not in emptied.requested
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM media_part WHERE rating_key = ?", (RATING_KEY,)
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM media_stream WHERE part_id = 31001").fetchone()[0] == 0
    )
