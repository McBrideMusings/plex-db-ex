"""Recording the files and tracks Plex lists for each movie and episode.

`media_part` and `media_stream` are facts about a title that only Plex carries:
which file plays it, in what container and codecs, and which audio and subtitle
tracks the file holds. The section listing `walk` reads already names every
title's `updatedAt` and its parts, but not the tracks; those come from
`/library/metadata/<ratingKey>`, one request per title.

**Fetch once per change.** A title is fetched only when the `updatedAt` its
listing record reports differs from the one stored with its parts, so a second
run over an unchanged library makes no metadata request at all. A record that
reports no `updatedAt` cannot be compared and is fetched every run.

**Snapshots.** A fetch replaces the whole set of parts and streams recorded for
that Plex record: a part Plex stopped listing is dropped, and a part's tracks are
rewritten rather than appended to. A record whose listing now names no file drops
its parts without a request. Each record commits on its own, so a run that stops
partway keeps what it fetched and the next run carries on.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .plex_client import PLEX_TYPE_EPISODE, PLEX_TYPE_MOVIE, PlexMediaSource

#: Plex's `streamType` numbering -> `media_stream.kind`. Any other stream type
#: (lyrics) is not recorded.
_STREAM_KINDS = {1: "video", 2: "audio", 3: "subtitle"}

#: Plex section type -> the `(items.type, Plex item type)` that carries files. A show
#: record has no file of its own; its episodes do.
_FILE_PASS = {"movie": ("movie", PLEX_TYPE_MOVIE), "show": ("episode", PLEX_TYPE_EPISODE)}


@dataclass
class MediaWalkStats:
    """What one media walk did — the summary `plexdb media-walk` prints."""

    titles_listed: int = 0
    #: Titles whose stored `updatedAt` matched, so nothing was requested.
    unchanged: int = 0
    #: Metadata requests made.
    fetched: int = 0
    parts_written: int = 0
    streams_written: int = 0
    #: Titles that dropped their parts because Plex lists no file for them now.
    emptied: int = 0
    #: Rating keys Plex returned no record for when fetched.
    missing: int = 0


def _int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _listed_parts(record: dict[str, Any]) -> bool:
    return any(
        part.get("file") for media in record.get("Media") or [] for part in media.get("Part") or []
    )


def _write_parts(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    rating_key: str,
    record: dict[str, Any],
    updated_at: int | None,
    now: str,
) -> tuple[int, int]:
    """Replace the parts and streams recorded for `rating_key` with `record`'s."""
    _drop_parts(conn, rating_key)
    parts = streams = 0
    for media in record.get("Media") or []:
        for part in media.get("Part") or []:
            part_id = _int(part.get("id"))
            file = part.get("file")
            if part_id is None or not file:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO media_part (part_id, item_id, rating_key, file, size,"
                " container, video_codec, audio_codec, height, bitrate, duration_ms,"
                " plex_updated_at, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    part_id,
                    item_id,
                    rating_key,
                    str(file),
                    _int(part.get("size")),
                    _text(part.get("container") or media.get("container")),
                    _text(media.get("videoCodec")),
                    _text(media.get("audioCodec")),
                    _int(media.get("height")),
                    _int(media.get("bitrate")),
                    _int(part.get("duration") or media.get("duration")),
                    updated_at,
                    now,
                ),
            )
            parts += 1
            conn.execute("DELETE FROM media_stream WHERE part_id = ?", (part_id,))
            for position, stream in enumerate(part.get("Stream") or []):
                kind = _STREAM_KINDS.get(_int(stream.get("streamType")) or 0)
                if kind is None:
                    continue
                idx = _int(stream.get("index"))
                conn.execute(
                    "INSERT OR REPLACE INTO media_stream (part_id, idx, kind, codec, lang,"
                    " forced, title) VALUES (?,?,?,?,?,?,?)",
                    (
                        part_id,
                        position if idx is None else idx,
                        kind,
                        _text(stream.get("codec")),
                        _text(stream.get("languageCode") or stream.get("language")),
                        1 if stream.get("forced") in (1, "1", True) else 0,
                        _text(stream.get("displayTitle") or stream.get("title")),
                    ),
                )
                streams += 1
    return parts, streams


def _drop_parts(conn: sqlite3.Connection, rating_key: str) -> None:
    conn.execute(
        "DELETE FROM media_stream WHERE part_id IN "
        "(SELECT part_id FROM media_part WHERE rating_key = ?)",
        (rating_key,),
    )
    conn.execute("DELETE FROM media_part WHERE rating_key = ?", (rating_key,))


def media_walk(
    conn: sqlite3.Connection, source: PlexMediaSource, *, section_key: str | None = None
) -> MediaWalkStats:
    """Record the parts and streams of every movie and episode whose Plex
    `updatedAt` differs from the one stored."""
    stats = MediaWalkStats()
    now = datetime.now(UTC).isoformat(timespec="seconds")

    item_for_key: dict[str, tuple[str, str]] = {
        row["rating_key"]: (row["item_id"], row["type"])
        for row in conn.execute(
            "SELECT p.rating_key AS rating_key, p.item_id AS item_id, i.type AS type "
            "FROM plex_items p JOIN items i ON i.item_id = p.item_id"
        )
    }
    stored: dict[str, set[int | None]] = {}
    for row in conn.execute("SELECT rating_key, plex_updated_at FROM media_part"):
        stored.setdefault(row["rating_key"], set()).add(row["plex_updated_at"])

    for section in source.sections():
        if section_key is not None and section.key != section_key:
            continue
        file_pass = _FILE_PASS.get(section.type)
        if file_pass is None:
            continue
        kind, plex_type = file_pass
        for listed in source.items(section.key, plex_type):
            rating_key = _text(listed.get("ratingKey"))
            known = item_for_key.get(rating_key or "")
            if rating_key is None or known is None or known[1] != kind:
                continue
            stats.titles_listed += 1
            item_id = known[0]
            updated_at = _int(listed.get("updatedAt"))
            if not _listed_parts(listed):
                if rating_key in stored:
                    with conn:
                        _drop_parts(conn, rating_key)
                    stats.emptied += 1
                continue
            if updated_at is not None and stored.get(rating_key) == {updated_at}:
                stats.unchanged += 1
                continue
            stats.fetched += 1
            record = source.metadata(rating_key)
            if record is None:
                stats.missing += 1
                with conn:
                    _drop_parts(conn, rating_key)
                continue
            with conn:
                parts, streams = _write_parts(
                    conn,
                    item_id=item_id,
                    rating_key=rating_key,
                    record=record,
                    updated_at=updated_at,
                    now=now,
                )
            stats.parts_written += parts
            stats.streams_written += streams
    return stats
