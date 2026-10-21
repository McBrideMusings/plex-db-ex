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
) -> CheckStats:
    """Probe every part with no `header` verdict, at most `limit` of them."""
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
    return stats
