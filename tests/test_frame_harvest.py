"""The Python side of the frame harvester: the window loop and the binary it runs, the
path mapping, and the frame cut the Search tab uses. The harvest itself is
`crates/plexdb/tests/harvest_frames.rs`."""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from plexdb import frame_harvest
from plexdb.config import Config
from plexdb.errors import ConfigError, MediaError
from plexdb.rust import RustStepError


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("PLEXDB_PATH", str(tmp_path / "plexdb.db"))
    monkeypatch.setenv("PLEXDB_FRAME_HOURS", "01:00-07:00")
    return Config.from_env()


def clock_at(hour: int, step: timedelta = timedelta(0)) -> Any:
    state = {"now": datetime(2026, 10, 16, hour, 0).astimezone()}

    def now() -> datetime:
        current = state["now"]
        state["now"] = current + step
        return current

    return now


def test_run_in_windows_runs_only_inside_hours_and_waits_out_an_empty_window(
    config: Config,
) -> None:
    calls: list[int] = []
    sleeps: list[float] = []
    clock = clock_at(0, timedelta(minutes=30))  # 00:00 outside, 00:30 outside, 01:00 inside ...

    def run_once() -> str:
        calls.append(1)
        return "nothing to do"

    frame_harvest.run_in_windows(
        config,
        run_once,
        clock=clock,
        sleep=sleeps.append,
        log=lambda _l: None,
        iterations=3,
    )
    assert calls == [1]
    assert sleeps[:2] == [60.0, 60.0]


def _binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    binary = tmp_path / "plexdb-rs"
    binary.write_text(f"#!/bin/sh\n{script}\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PLEXDB_RS_BIN", str(binary))


def test_a_harvest_hands_each_line_on_and_returns_why_the_binary_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _binary(
        tmp_path,
        monkeypatch,
        'test "$1" = harvest-frames || exit 2; echo "frames: one"; '
        'echo "harvest-frames stopped: encoder unavailable; 2 indexed, 0 failed"',
    )
    lines: list[str] = []
    assert frame_harvest.harvest(Config.from_env(), lines.append) == "encoder unavailable"
    assert lines == [
        "frames: one",
        "harvest-frames stopped: encoder unavailable; 2 indexed, 0 failed",
    ]


def test_a_harvest_the_binary_fails_raises_its_error_line_and_an_unset_setting_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _binary(tmp_path, monkeypatch, "echo 'error: another harvester is running' >&2; exit 1")
    with pytest.raises(RustStepError, match="another harvester is running"):
        frame_harvest.harvest(Config.from_env(), lambda _l: None)
    _binary(tmp_path, monkeypatch, "echo 'error: PLEXDB_MEDIA_DIR is not set' >&2; exit 78")
    with pytest.raises(ConfigError, match="PLEXDB_MEDIA_DIR is not set"):
        frame_harvest.harvest(Config.from_env(), lambda _l: None)


def test_a_plex_path_is_read_under_the_media_dir_and_never_outside_it(tmp_path: Path) -> None:
    media = tmp_path / "library"
    (media / "movies").mkdir(parents=True)
    roots = ("/media",)
    got = frame_harvest.local_path(media, "/media/movies/Heat (1995).mkv", roots)
    assert got == media / "movies" / "Heat (1995).mkv"
    for plex_file in ("/media/../etc/passwd", "/other/movies/x.mkv", "/mediacache/x.mkv", ""):
        with pytest.raises(MediaError):
            frame_harvest.local_path(media, plex_file, roots)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs a real ffmpeg")
def test_a_time_past_the_last_frame_costs_only_its_own_jpeg(tmp_path: Path) -> None:
    video = tmp_path / "a.mp4"
    make = [
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=10:s=320x240:r=24",
        "-c:v", "libx264", "-g", "48", "-y", str(video),
    ]  # fmt: skip
    subprocess.run(make, check=True)
    media = frame_harvest.FfmpegMedia(ffmpeg="ffmpeg", ffprobe="ffprobe")
    got = media.frames(video, [20000, 1000, 9990, 5000, 10500, 3000])
    assert [g is not None for g in got] == [False, True, False, True, False, True]
