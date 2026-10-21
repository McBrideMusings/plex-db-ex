"""Checking each file Plex lists against the rules a healthy file keeps.

`media_check` holds one verdict per `(part_id, kind)`. This module writes the `header`
kind: ffprobe reads the file's container and streams, and the file passes when it

- parses (ffprobe exits 0 with a report),
- is not empty on disk,
- has a video stream and an audio stream, and
- has a duration within 2% of the one Plex recorded for it.

**Probe once per change.** A part is probed only when it has no `header` row.
`media_walk` replaces a changed record's parts whole, and a part's checks go with it
(`ON DELETE CASCADE`), so a part that is new, or whose size or `plex_updated_at` changed
because Plex re-read it, is the one with no row. A part a run did not judge (the file is
not on the media mount, ffprobe is missing or timed out) gets no row either, so the next
run tries it again rather than recording a mount that was down as a damaged file.

**Decode a batch a night.** The `decode` kind runs ffmpeg over every frame of a file and
fails it on any error ffmpeg prints. A full decode reads the whole file, so a run decodes
at most `decode_batch` parts: the ones never decoded first, then the one decoded longest
ago. A re-decode replaces the part's row, so the whole library cycles through the batch.
A part the run could not decode gets no row and does not use up the batch, so parts the
mount lacks never crowd out the ones it has.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import MediaError
from .frame_harvest import FfmpegMedia

#: A file's duration may differ from Plex's by this fraction before it fails.
DURATION_TOLERANCE = 0.02


@dataclass
class CheckStats:
    """What one `check-files` run did."""

    checked: int = 0
    passed: int = 0
    failed: int = 0
    #: Parts left unchecked for the next run: not on the mount, or ffprobe could not judge.
    skipped: int = 0
    decoded: int = 0
    decode_failed: int = 0
    #: Parts the decode pass could not judge (not on the mount, ffmpeg missing or timed out).
    decode_skipped: int = 0


def _duration_ms(report: dict[str, Any]) -> int | None:
    try:
        return round(float(report["format"]["duration"]) * 1000)
    except (KeyError, TypeError, ValueError):
        return None


def rule_failures(report: dict[str, Any], *, plex_duration_ms: int | None) -> list[str]:
    """Every rule `report` (ffprobe's `-show_format -show_streams` JSON) breaks."""
    failures: list[str] = []
    kinds = {s.get("codec_type") for s in report.get("streams") or [] if isinstance(s, dict)}
    if "video" not in kinds:
        failures.append("no video stream")
    if "audio" not in kinds:
        failures.append("no audio stream")
    if plex_duration_ms:
        found = _duration_ms(report)
        if found is None:
            failures.append(f"no duration; Plex says {plex_duration_ms / 1000:.1f} s")
        elif abs(found - plex_duration_ms) > plex_duration_ms * DURATION_TOLERANCE:
            off = abs(found - plex_duration_ms) * 100 / plex_duration_ms
            failures.append(
                f"duration {found / 1000:.1f} s is {off:.1f}% from Plex's "
                f"{plex_duration_ms / 1000:.1f} s"
            )
    return failures


def judge(media: FfmpegMedia, path: Path, *, plex_duration_ms: int | None) -> str | None:
    """`None` when the file at `path` passes, else why it fails. A `MediaError` or
    `OSError` means the file was not judged."""
    if path.stat().st_size == 0:
        return "file is empty"
    report, why = media.probe(path)
    if report is None:
        return f"does not parse: {why}"
    failures = rule_failures(report, plex_duration_ms=plex_duration_ms)
    return "; ".join(failures) or None


def check_files(
    conn: sqlite3.Connection,
    media: FfmpegMedia,
    locate: Callable[[str], Path],
    *,
    limit: int | None = None,
    decode_batch: int = 0,
) -> CheckStats:
    """Probe every part with no `header` verdict, at most `limit` of them, then decode
    `decode_batch` parts in full."""
    stats = CheckStats()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    pending = conn.execute(
        # Plex gives a record with several parts one duration for the whole title, so a
        # part's own length is compared only when it is the record's single part.
        "SELECT p.part_id, p.file, CASE WHEN (SELECT COUNT(*) FROM media_part o"
        " WHERE o.rating_key = p.rating_key) = 1 THEN p.duration_ms END"
        " FROM media_part p WHERE NOT EXISTS "
        "(SELECT 1 FROM media_check c WHERE c.part_id = p.part_id AND c.kind = 'header') "
        "ORDER BY p.part_id"
    ).fetchall()
    for part_id, file, plex_duration_ms in pending:
        if limit is not None and stats.checked >= limit:
            break
        try:
            error = judge(media, locate(file), plex_duration_ms=plex_duration_ms)
        except (MediaError, OSError):
            stats.skipped += 1
            continue
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO media_check (part_id, kind, checked_at, ok, error)"
                " VALUES (?, 'header', ?, ?, ?)",
                (part_id, now, 0 if error else 1, error),
            )
        stats.checked += 1
        if error:
            stats.failed += 1
        else:
            stats.passed += 1
    _decode_batch(conn, media, locate, decode_batch, stats, now)
    return stats


def _decode_batch(
    conn: sqlite3.Connection,
    media: FfmpegMedia,
    locate: Callable[[str], Path],
    batch: int,
    stats: CheckStats,
    now: str,
) -> None:
    """Decode the `batch` parts whose last decode is oldest, never-decoded ones first."""
    if batch <= 0:
        return
    parts = conn.execute(
        "SELECT p.part_id, p.file FROM media_part p LEFT JOIN media_check c"
        " ON c.part_id = p.part_id AND c.kind = 'decode'"
        " ORDER BY c.checked_at IS NOT NULL, c.checked_at, p.part_id"
    ).fetchall()
    for part_id, file in parts:
        if stats.decoded >= batch:
            break
        try:
            path = locate(file)
            path.stat()  # raises OSError for a file not on the mount, before ffmpeg runs
            error = media.decode(path)
        except (MediaError, OSError):
            stats.decode_skipped += 1
            continue
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO media_check (part_id, kind, checked_at, ok, error)"
                " VALUES (?, 'decode', ?, ?, ?)",
                (part_id, now, 0 if error else 1, error or None),
            )
        stats.decoded += 1
        if error:
            stats.decode_failed += 1
