"""`check_files` judges real files with a real ffprobe: generated valid, truncated, silent and
empty fixtures give the verdicts the rules name, and a part already judged is not probed again."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from plexdb.explore import RECIPES
from plexdb.frame_harvest import FfmpegMedia
from plexdb.media_check import check_files
from plexdb.store import init, open_store

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

    recipe = next(r for r in RECIPES if r["name"] == "Damaged or rule-breaking files")
    assert recipe["group"] == "Library health"
    rows = conn.execute(recipe["sql"]).fetchall()

    assert [str(clips["truncated"]) in " ".join(map(str, row)) for row in rows] == [True]
