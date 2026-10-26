"""`check_files` judges real files with a real ffprobe: generated valid, truncated, silent and
empty fixtures give the verdicts the rules name, and a part already judged is not probed again."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fixture_store import init

from plexdb.errors import MediaTimeout
from plexdb.frame_harvest import FfmpegMedia
from plexdb.media_check import check_files
from plexdb.panels import DEFAULT_DASHBOARDS
from plexdb.store import open_store

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are needed to generate and probe the fixtures",
)

#: The generated clips run this long.
SECONDS = 2
DURATION_MS = SECONDS * 1000


class CountingMedia(FfmpegMedia):
    """A real `FfmpegMedia` that remembers which files it probed."""

    def __init__(self) -> None:
        super().__init__(ffmpeg="ffmpeg", ffprobe="ffprobe")
        self.probed: list[Path] = []

    def probe(self, path: Path):  # type: ignore[no-untyped-def]
        self.probed.append(path)
        return super().probe(path)


def _ffmpeg(*args: str) -> None:
    subprocess.run(  # noqa: S603
        ["ffmpeg", "-nostdin", "-v", "error", "-y", *args],  # noqa: S607
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="module")
def clips(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("clips")
    video = ["-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration={SECONDS}"]
    audio = ["-f", "lavfi", "-i", f"sine=frequency=440:duration={SECONDS}"]
    valid = root / "valid.mp4"
    _ffmpeg(*video, *audio, "-c:v", "libx264", "-c:a", "aac", "-shortest", str(valid))
    silent = root / "silent.mp4"
    _ffmpeg(*video, "-c:v", "libx264", str(silent))
    truncated = root / "truncated.mp4"
    truncated.write_bytes(valid.read_bytes()[: valid.stat().st_size // 2])
    empty = root / "empty.mp4"
    empty.write_bytes(b"")
    return {"valid": valid, "silent": silent, "truncated": truncated, "empty": empty}


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    store = tmp_path / "plexdb.db"
    init(store)
    with open_store(store) as connection:
        connection.execute("INSERT INTO items (item_id, type, title) VALUES ('t:1', 'movie', 'M')")
        yield connection


def _add_part(conn: sqlite3.Connection, part_id: int, file: Path, duration_ms: int | None) -> None:
    conn.execute(
        "INSERT INTO media_part (part_id, item_id, rating_key, file, duration_ms, fetched_at)"
        " VALUES (?, 't:1', ?, ?, ?, '2026-10-21T00:00:00+00:00')",
        (part_id, str(part_id), str(file), duration_ms),
    )
    conn.commit()


def _verdict(conn: sqlite3.Connection, part_id: int) -> tuple[int, str | None]:
    row = conn.execute(
        "SELECT ok, error FROM media_check WHERE part_id = ? AND kind = 'header'", (part_id,)
    ).fetchone()
    return (row[0], row[1])


def _run(conn: sqlite3.Connection, media: FfmpegMedia | None = None):  # type: ignore[no-untyped-def]
    return check_files(conn, media or CountingMedia(), Path)


def test_a_valid_file_passes_and_a_truncated_one_fails_with_ffprobes_error(
    conn: sqlite3.Connection, clips: dict[str, Path]
) -> None:
    _add_part(conn, 1, clips["valid"], DURATION_MS)
    _add_part(conn, 2, clips["truncated"], DURATION_MS)

    stats = _run(conn)

    assert (stats.checked, stats.passed, stats.failed) == (2, 1, 1)
    assert _verdict(conn, 1) == (1, None)
    ok, error = _verdict(conn, 2)
    assert ok == 0
    assert error is not None and error.startswith("does not parse: ")
    assert str(clips["truncated"]) not in error


def test_a_part_with_a_verdict_is_not_probed_again_until_its_row_is_replaced(
    conn: sqlite3.Connection, clips: dict[str, Path]
) -> None:
    _add_part(conn, 1, clips["valid"], DURATION_MS)
    first = CountingMedia()
    _run(conn, first)
    second = CountingMedia()
    again = _run(conn, second)

    assert first.probed == [clips["valid"]]
    assert second.probed == [] and again.checked == 0

    # Plex re-reading a changed record replaces its parts, and the verdict goes with them.
    conn.execute("DELETE FROM media_part WHERE part_id = 1")
    _add_part(conn, 1, clips["valid"], DURATION_MS)
    third = CountingMedia()
    _run(conn, third)
    assert third.probed == [clips["valid"]]


@pytest.mark.parametrize(
    ("clip", "plex_ms", "ok", "mentions"),
    [
        ("valid", DURATION_MS * 1.019, 1, None),
        ("valid", DURATION_MS * 1.05, 0, "duration"),
        ("silent", DURATION_MS, 0, "no audio stream"),
        ("empty", DURATION_MS, 0, "file is empty"),
    ],
)
def test_each_rule_fails_the_file_that_breaks_it(
    conn: sqlite3.Connection,
    clips: dict[str, Path],
    clip: str,
    plex_ms: float,
    ok: int,
    mentions: str | None,
) -> None:
    _add_part(conn, 1, clips[clip], round(plex_ms))
    _run(conn)

    got_ok, error = _verdict(conn, 1)
    assert got_ok == ok
    if mentions is None:
        assert error is None
    else:
        assert error is not None and mentions in error


def test_a_part_of_a_multi_part_record_is_not_held_to_the_titles_duration(
    conn: sqlite3.Connection, clips: dict[str, Path]
) -> None:
    _add_part(conn, 1, clips["valid"], DURATION_MS * 2)
    conn.execute(
        "INSERT INTO media_part (part_id, item_id, rating_key, file, duration_ms, fetched_at)"
        " VALUES (2, 't:1', '1', ?, ?, 'x')",
        (str(clips["valid"]), DURATION_MS * 2),
    )
    conn.commit()
    _run(conn)

    assert _verdict(conn, 1) == (1, None)
    assert _verdict(conn, 2) == (1, None)


def test_a_file_missing_from_the_mount_is_left_for_the_next_run(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _add_part(conn, 1, tmp_path / "not-mounted.mp4", DURATION_MS)

    stats = _run(conn)

    assert (stats.checked, stats.skipped) == (0, 1)
    assert conn.execute("SELECT COUNT(*) FROM media_check").fetchone()[0] == 0


def test_the_damaged_files_recipe_lists_the_bad_file_and_not_the_good_one(
    conn: sqlite3.Connection, clips: dict[str, Path]
) -> None:
    _add_part(conn, 1, clips["valid"], DURATION_MS)
    _add_part(conn, 2, clips["truncated"], DURATION_MS)
    _run(conn)

    recipe = _health_panel("Damaged or rule-breaking files")
    rows = conn.execute(recipe["sql"]).fetchall()

    assert [str(clips["truncated"]) in " ".join(map(str, row)) for row in rows] == [True]


def _health_panel(title: str) -> dict[str, Any]:
    return next(p for d in DEFAULT_DASHBOARDS for p in d["panels"] if p["title"] == title)


def _decode_rows(conn: sqlite3.Connection) -> dict[int, tuple[int, str | None, str]]:
    return {
        r[0]: (r[1], r[2], r[3])
        for r in conn.execute(
            "SELECT part_id, ok, error, checked_at FROM media_check WHERE kind = 'decode'"
        )
    }


def test_a_truncated_file_fails_decode_with_ffmpegs_error_text_stored(
    conn: sqlite3.Connection, clips: dict[str, Path]
) -> None:
    _add_part(conn, 1, clips["valid"], DURATION_MS)
    _add_part(conn, 2, clips["truncated"], DURATION_MS)

    stats = check_files(conn, CountingMedia(), Path, decode_batch=10)

    rows = _decode_rows(conn)
    assert (stats.decoded, stats.decode_failed) == (2, 1)
    assert rows[1][:2] == (1, None)
    assert rows[2][0] == 0
    assert rows[2][1] and "moov atom not found" in rows[2][1]
    assert str(clips["truncated"]) not in rows[2][1]


_OLD = {1: "2026-10-03T00:00:00+00:00", 2: "2026-10-01T00:00:00+00:00"}


def test_decode_takes_never_decoded_parts_first_then_the_oldest_up_to_the_batch(
    conn: sqlite3.Connection, clips: dict[str, Path]
) -> None:
    for part_id in (1, 2, 3, 4):
        _add_part(conn, part_id, clips["valid"], DURATION_MS)
    conn.executemany(
        "INSERT INTO media_check (part_id, kind, checked_at, ok) VALUES (?, 'decode', ?, 1)",
        list(_OLD.items()),
    )
    conn.commit()

    stats = check_files(conn, CountingMedia(), Path, decode_batch=3)

    rows = _decode_rows(conn)
    assert stats.decoded == 3
    # 3 and 4 were never decoded; of the two decoded before, part 2 is older than part 1.
    redone = {part_id for part_id, row in rows.items() if row[2] != _OLD.get(part_id)}
    assert redone == {2, 3, 4}


def test_a_batch_of_zero_decodes_nothing(conn: sqlite3.Connection, clips: dict[str, Path]) -> None:
    _add_part(conn, 1, clips["valid"], DURATION_MS)

    stats = check_files(conn, CountingMedia(), Path, decode_batch=0)

    assert stats.decoded == 0 and _decode_rows(conn) == {}


def test_decode_progress_recipe_counts_decoded_parts_and_the_oldest_date(
    conn: sqlite3.Connection, clips: dict[str, Path]
) -> None:
    for part_id in (1, 2, 3):
        _add_part(conn, part_id, clips["valid"], DURATION_MS)
    conn.execute(
        "INSERT INTO media_check (part_id, kind, checked_at, ok) VALUES"
        " (1, 'decode', '2026-10-01T00:00:00+00:00', 1),"
        " (2, 'decode', '2026-10-05T00:00:00+00:00', 1)"
    )
    conn.commit()

    recipe = _health_panel("Decode progress")
    row = conn.execute(recipe["sql"]).fetchone()
    assert tuple(row) == (2, 3, "2026-10-01T00:00:00+00:00")


def test_parts_missing_from_the_mount_do_not_use_up_the_decode_batch(
    conn: sqlite3.Connection, clips: dict[str, Path], tmp_path: Path
) -> None:
    for part_id in (1, 2):
        _add_part(conn, part_id, tmp_path / f"gone{part_id}.mp4", DURATION_MS)
    _add_part(conn, 3, clips["valid"], DURATION_MS)

    stats = check_files(conn, CountingMedia(), Path, decode_batch=1)

    assert (stats.decoded, stats.decode_skipped) == (1, 2)
    assert set(_decode_rows(conn)) == {3}


class SlowFirstPartMedia(CountingMedia):
    """Decoding the first file it is handed runs past the time limit."""

    def __init__(self) -> None:
        super().__init__()
        self.decoded_files: list[Path] = []

    def decode(self, path: Path) -> str:
        self.decoded_files.append(path)
        if len(self.decoded_files) == 1:
            raise MediaTimeout(f"{path}: decode: ffmpeg ran past 21600 s", "ffmpeg timed out")
        return super().decode(path)


def test_a_decode_that_times_out_is_recorded_and_the_next_run_starts_elsewhere(
    conn: sqlite3.Connection, clips: dict[str, Path]
) -> None:
    for part_id, name in ((1, "valid"), (2, "silent")):
        _add_part(conn, part_id, clips[name], DURATION_MS)

    first = SlowFirstPartMedia()
    stats = check_files(conn, first, Path, decode_batch=1)

    assert first.decoded_files == [clips["valid"]]
    assert (stats.decoded, stats.decode_failed, stats.decode_timed_out) == (1, 0, 1)
    assert stats.decode_skipped == 0
    assert _decode_rows(conn)[1][:2] == (0, "ffmpeg timed out")
    damaged = next(
        p["sql"] for d in DEFAULT_DASHBOARDS for p in d["panels"] if p["id"] == "damaged-files"
    )
    # The silent clip's header check fails on its own; the timed-out decode adds no row.
    assert [(r["kind"], r["file"]) for r in conn.execute(damaged)] == [
        ("header", str(clips["silent"]))
    ]

    second = SlowFirstPartMedia()
    check_files(conn, second, Path, decode_batch=1)

    assert second.decoded_files == [clips["silent"]]
