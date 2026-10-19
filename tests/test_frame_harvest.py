"""The frame harvester: order, sampling, one transaction per title, and stopping."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from plexdb import frame_harvest, frames
from plexdb.config import Config
from plexdb.errors import HarvesterBusy, MediaError
from plexdb.frame_encoder import EncoderClient
from plexdb.frame_harvest import Cut, Scan
from plexdb.plex_client import Section
from plexdb.store import init, open_store

# item_id, type, section, addedAt
TITLES = [
    ("imdb:old", "movie", "1", 100),
    ("imdb:new", "movie", "1", 300),
    ("tvdb:ep", "episode", "2", 900),
    ("imdb:mid", "movie", "1", 200),
    ("imdb:chg", "movie", "1", 50),
]


class FakePlex:
    def sections(self) -> list[Section]:
        return [Section("1", "movie", "Movies"), Section("2", "show", "Shows")]

    def items(self, section_key: str, type_: int) -> list[dict[str, Any]]:
        return [
            {
                "ratingKey": f"rk-{item_id}",
                "addedAt": added,
                "Media": [
                    {
                        "duration": 12_000,
                        "Part": [{"id": i + 1, "size": 1000 + i, "file": f"/media/p{i + 1}.mkv"}],
                    }
                ],
            }
            for i, (item_id, _, section, added) in enumerate(TITLES)
            if section == section_key
        ]


def _part(path: Path) -> int:
    return int(path.stem[1:])


class FakeMedia:
    """No decoding: every title has Cuts at 3 s and 5 s and its last video frame at 10 s,
    though Plex reports 12 s, as a file with audio past its video does. Part N's file is
    `p<N>.mkv`, so it answers by part id."""

    def __init__(self) -> None:
        self.hashes: dict[int, str] = {}
        self.fail_parts: set[int] = set()
        self.damaged_parts: set[int] = set()
        self.read: list[int] = []
        self.asked: list[int] = []

    def file_hash(self, path: Path, size: int) -> str:
        part_id = _part(path)
        return self.hashes.get(part_id, f"h{part_id}")

    def scan(self, path: Path) -> Scan:
        part_id = _part(path)
        self.read.append(part_id)
        if part_id in self.fail_parts:
            raise MediaError(f"part {part_id}: ffmpeg scdet exited 1", "ffmpeg scene scan exited 1")
        if part_id in self.damaged_parts:
            raise MediaError(
                f"part {part_id}: source file damaged (3 decode errors, cpu decode)",
                "source file damaged (3 decode errors)",
            )
        return Scan([Cut(3000, 22.5, "scdet"), Cut(5000, None, "chapter")], 10_000, 2)

    def frames(self, path: Path, times_ms: Sequence[int]) -> list[bytes | None]:
        self.asked += times_ms
        return [b"\xff\xd8jpeg" if t_ms <= 10_000 else None for t_ms in times_ms]


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    store = tmp_path / "plexdb.db"
    init(store)
    with open_store(store) as conn:
        for item_id, kind, section, _ in TITLES:
            conn.execute(
                "INSERT INTO items (item_id, type, title) VALUES (?, ?, ?)",
                (item_id, kind, item_id),
            )
            conn.execute(
                "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
                "VALUES (?, ?, ?, '2026-10-15T00:00:00+00:00')",
                (f"rk-{item_id}", item_id, section),
            )
        conn.commit()
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("PLEXDB_FRAMES_DIR", str(tmp_path / "frames"))
    monkeypatch.setenv("PLEXDB_FRAME_HOURS", "01:00-07:00")
    monkeypatch.setenv("PLEXDB_FRAME_MODEL", "clip-test")
    monkeypatch.setenv("PLEXDB_MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("PLEX_SOURCE_ROOTS", "/media")
    return Config.from_env()


def encoder_with(status: int = 200, seen: list[int] | None = None) -> EncoderClient:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if seen is not None:
            seen.append(len(body["input"]))
        if status != 200:
            return httpx.Response(status, text="no")
        data = [
            {"index": i, "embedding": [3.0] * frames.FRAME_DIMS} for i in range(len(body["input"]))
        ]
        return httpx.Response(200, json={"model": "clip-test", "data": data})

    http = httpx.Client(transport=httpx.MockTransport(handle))
    return EncoderClient("http://enc", "clip-test", http=http, log=lambda _l: None)


def clock_at(hour: int, step: timedelta = timedelta(0)) -> Any:
    state = {"now": datetime(2026, 10, 16, hour, 0).astimezone()}

    def now() -> datetime:
        current = state["now"]
        state["now"] = current + step
        return current

    return now


def run(
    config: Config, media: FakeMedia, encoder: EncoderClient, **kw: Any
) -> frame_harvest.HarvestResult:
    logs: list[str] = []
    kw.setdefault("now_fn", clock_at(3))
    result = frame_harvest.harvest(
        config, plex=FakePlex(), media=media, encoder=encoder, log=logs.append, **kw
    )
    result.logs = logs  # type: ignore[attr-defined]
    return result


def rows(config: Config, sql: str, *args: object) -> list[sqlite3.Row]:
    with frames.open_frames(frames_path(config)) as conn:
        return conn.execute(sql, args).fetchall()


def frames_path(config: Config) -> Path:
    assert config.frames_path is not None
    return config.frames_path


def seed(config: Config, item_id: str, file_hash: str, error: str | None = None) -> None:
    with frames.open_frames(frames_path(config)) as conn:
        conn.execute(
            "INSERT INTO frame_index_state (item_id, file_hash, indexed_at, error) VALUES (?, ?, ?, ?)",  # noqa: E501
            (item_id, file_hash, "2026-10-01T00:00:00+00:00", error),
        )
        conn.commit()


def test_order_is_queued_then_new_movies_by_newest_then_episodes_then_changed(
    config: Config,
) -> None:
    media = FakeMedia()
    seed(config, "imdb:chg", "stale-hash")
    with frames.open_frames(frames_path(config)) as conn:
        frames.queue(conn, "imdb:old", known=True, by="t", now=datetime(2026, 10, 1).astimezone())
    result = run(config, media, encoder_with())
    assert result.titles == ["imdb:old", "imdb:new", "imdb:mid", "tvdb:ep", "imdb:chg"]
    assert result.stopped == "nothing to do"
    assert rows(config, "SELECT count(*) FROM frame_queue")[0][0] == 0


def test_an_unchanged_indexed_title_and_a_failed_one_are_not_visited_again(config: Config) -> None:
    media = FakeMedia()
    seed(config, "imdb:chg", "h5")
    seed(config, "imdb:old", "h1", error="ffmpeg exited 1")
    result = run(config, media, encoder_with())
    assert "imdb:chg" not in result.titles and "imdb:old" not in result.titles


def test_a_title_writes_cuts_one_vector_per_sampled_frame_and_its_state(config: Config) -> None:
    media = FakeMedia()
    run(config, media, encoder_with(), limit=1)
    item = "imdb:new"
    assert [
        tuple(r)
        for r in rows(
            config, "SELECT t_ms, score, source FROM cuts WHERE item_id = ? ORDER BY t_ms", item
        )
    ] == [
        (3000, 22.5, "scdet"),
        (5000, None, "chapter"),
    ]
    # Shots 0-3 s (> 2 s): one frame 1 s in; 3-5 s: its midpoint; 5-10 s: 6 s and 8 s. The
    # last Shot ends at the last video frame, not at Plex's 12 s, so nothing past it is asked.
    vec = rows(config, "SELECT t_ms, shot_ix FROM frame_vec WHERE item_id = ? ORDER BY t_ms", item)
    assert [tuple(r) for r in vec] == [(1000, 0), (4000, 1), (6000, 2), (8000, 2)]
    assert media.asked == [1000, 4000, 6000, 8000]
    shot_rows = rows(
        config, "SELECT shot_ix, in_ms, out_ms FROM shots WHERE item_id = ? ORDER BY shot_ix", item
    )
    assert [tuple(r) for r in shot_rows] == [(0, 0, 3000), (1, 3000, 5000), (2, 5000, 10_000)]
    state = rows(
        config,
        "SELECT shots, vectors, dims, quant, error, duration_ms FROM frame_index_state WHERE item_id = ?",  # noqa: E501
        item,
    )[0]
    assert tuple(state) == (3, len(vec), 768, "int8", None, 10_000)


def test_a_second_harvester_refuses_and_names_the_first(config: Config) -> None:
    refusals: list[str] = []

    class Rival(FakeMedia):
        def scan(self, path: Path) -> Scan:
            if not refusals:
                with pytest.raises(HarvesterBusy) as busy:
                    run(config, FakeMedia(), encoder_with(), limit=1)
                refusals.append(str(busy.value))
            return super().scan(path)

    run(config, Rival(), encoder_with(), limit=1)
    assert refusals == [f"another harvester is running (pid {os.getpid()}, indexing imdb:new)"]
    assert run(config, FakeMedia(), encoder_with(), limit=1).indexed == 1


def test_sample_times_and_shots() -> None:
    assert frame_harvest.shots([3000, 3100, 5000], 10_000) == [
        (0, 3000),
        (3000, 5000),
        (5000, 10_000),
    ]
    assert frame_harvest.sample_times([(0, 3000), (3000, 5000), (5000, 10_000)]) == [
        (1000, 0),
        (4000, 1),
        (6000, 2),
        (8000, 2),
    ]
    assert frame_harvest.sample_times([(0, 400)]) == [(200, 0)]


def test_a_refusing_encoder_stops_the_run_and_the_title_keeps_its_rows(config: Config) -> None:
    with frames.open_frames(frames_path(config)) as conn:
        conn.execute(
            "INSERT INTO frame_index_state (item_id, file_hash, indexed_at, shots, vectors) VALUES ('imdb:new', 'h2', '2026-10-01T00:00:00+00:00', 1, 1)"  # noqa: E501
        )
        conn.execute(
            "INSERT INTO cuts (item_id, t_ms, score, source) VALUES ('imdb:new', 999, 1.0, 'scdet')"
        )
        conn.execute(
            "INSERT INTO frame_vec (item_id, t_ms, shot_ix, vec) VALUES ('imdb:new', 500, 0, vec_quantize_int8(?, 'unit'))",  # noqa: E501
            (frame_harvest._vec_blob([1.0] + [0.0] * 767),),
        )
        frames.queue(conn, "imdb:new", known=True, by="t", now=datetime(2026, 10, 1).astimezone())
        conn.commit()
    for status in (503, 429):
        result = run(config, FakeMedia(), encoder_with(status))
        assert result.stopped == "encoder unavailable"
        assert (result.indexed, result.failed) == (0, 0)
        assert [
            tuple(r) for r in rows(config, "SELECT t_ms FROM frame_vec WHERE item_id = 'imdb:new'")
        ] == [(500,)]
        assert [
            tuple(r) for r in rows(config, "SELECT t_ms FROM cuts WHERE item_id = 'imdb:new'")
        ] == [(999,)]
        assert rows(config, "SELECT count(*) FROM frame_queue")[0][0] == 1
        assert (
            rows(config, "SELECT error FROM frame_index_state WHERE item_id = 'imdb:new'")[0][0]
            is None
        )
        assert rows(config, "SELECT count(*) FROM frame_harvest")[0][0] == 0


def test_an_unreadable_title_records_its_error_and_the_run_moves_on(config: Config) -> None:
    media = FakeMedia()
    media.fail_parts = {2}  # imdb:new
    result = run(config, media, encoder_with(), limit=2)
    assert (result.indexed, result.failed) == (1, 1)
    state = rows(
        config,
        "SELECT error, reason, file_hash FROM frame_index_state WHERE item_id = 'imdb:new'",
    )[0]
    assert "scdet exited 1" in state["error"] and state["file_hash"] == "h2"
    assert state["reason"] == "ffmpeg scene scan exited 1"
    assert any("item_id=imdb:new" in line and "error=" in line for line in result.logs)  # type: ignore[attr-defined]  # noqa: E501


def test_the_log_line_carries_the_fields_the_operator_reads(config: Config) -> None:
    result = run(config, FakeMedia(), encoder_with(), limit=1)
    (line,) = [row for row in result.logs if "item_id=imdb:new" in row and "part=" in row]  # type: ignore[attr-defined]  # noqa: E501
    for field in (
        "frames: 2026-",
        "part=2",
        "bytes=1001",
        "decode_s=",
        "scan_s=",
        "extract_s=",
        "encode_s=",
        "shots=3",
        "vectors=4",
    ):
        assert field in line


def test_each_stage_logs_a_start_line_before_the_title_line(config: Config) -> None:
    result = run(config, FakeMedia(), encoder_with(), limit=1)
    rows = [row for row in result.logs if "item_id=imdb:new" in row]  # type: ignore[attr-defined]
    assert [("stage=scan start" in r, "stage=extract start" in r) for r in rows[:2]] == [
        (True, False),
        (False, True),
    ]
    assert "part=2" in rows[2]


def test_the_default_log_flushes_each_line(capsys: pytest.CaptureFixture[str]) -> None:
    frame_harvest._flushed("frames: x")
    assert capsys.readouterr().out == "frames: x\n"


def test_outside_the_window_nothing_is_read_and_a_closing_window_stops_after_the_title(
    config: Config,
) -> None:
    media = FakeMedia()
    result = run(config, media, encoder_with(), now_fn=clock_at(12))
    assert (result.stopped, media.read) == ("window closed", [])
    # 06:00 start, 40 minutes a clock read: the window closes during the second title.
    result = run(config, media, encoder_with(), now_fn=clock_at(6, timedelta(minutes=12)))
    assert result.stopped == "window closed" and 1 <= len(result.titles) < len(TITLES)
    assert result.indexed == len(result.titles)


def test_status_names_the_title_in_hand_while_it_is_indexed(config: Config) -> None:
    seen: list[Any] = []

    class Spy(FakeMedia):
        def scan(self, path: Path) -> Scan:
            with frames.open_frames(frames_path(config)) as conn:
                seen.append(frames._indexing(conn))
            return super().scan(path)

    run(config, Spy(), encoder_with(), limit=1)
    assert seen[0]["item_id"] == "imdb:new"
    with frames.open_frames(frames_path(config)) as conn:
        assert frames._indexing(conn) is None


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


def test_parse_scdet_and_chapters() -> None:
    text = "frame:0 pts:1 pts_time:12.5\nlavfi.scd.score=14.25\nlavfi.scd.time=12.5\n"
    assert frame_harvest.parse_scdet(text) == [Cut(12500, 14.25, "scdet")]
    chapters = '{"chapters": [{"start_time": "0.000000"}, {"start_time": "301.5"}]}'
    assert frame_harvest.parse_chapters(chapters) == [Cut(301500, None, "chapter")]


def test_parse_last_frame_takes_the_largest_pts_in_the_time_base() -> None:
    crc = ["#software: Lavf", "#tb 0: 1/1000", "#media_type 0: video",
           "0,          0,          0,       42,   133632, 0x1",
           "0,      282662,      282662,       42,   133632, 0x2",
           "0,      282620,      282620,       42,   133632, 0x3"]  # fmt: skip
    assert frame_harvest.parse_last_frame(crc) == 282_662
    assert (
        frame_harvest.parse_last_frame(["#tb 0: 1001/24000", "0, 6779, 6779, 1, 9, 0x1"]) == 282_740
    )
    assert frame_harvest.parse_last_frame(["#tb 0: 1/1000"]) is None


def test_a_stalled_or_unreadable_scdet_answer_fails_the_title_not_the_run() -> None:
    with pytest.raises(MediaError):
        frame_harvest.parse_scdet("lavfi.scd.score=high\n")


def test_a_plex_path_is_read_under_the_media_dir_and_never_outside_it(tmp_path: Path) -> None:
    media = tmp_path / "library"
    (media / "movies").mkdir(parents=True)
    roots = ("/media",)
    got = frame_harvest.local_path(media, "/media/movies/Heat (1995).mkv", roots)
    assert got == media / "movies" / "Heat (1995).mkv"
    for plex_file in ("/media/../etc/passwd", "/other/movies/x.mkv", "/mediacache/x.mkv", ""):
        with pytest.raises(MediaError):
            frame_harvest.local_path(media, plex_file, roots)


# --- CUDA decode (PLEXDB_FRAME_DECODE) ---

_FAKE_FFMPEG = """#!/usr/bin/env python3
import sys
args = sys.argv[1:]
with open({log!r}, "a") as log:
    log.write(" ".join(args) + "\\n")
if "-hwaccel" in args and {cuda_fails!r}:
    sys.stderr.write("[AVHWDeviceContext] Cannot load libcuda.so.1\\n")
    sys.stderr.write("Device creation failed: -1.\\n")
    sys.exit(1)
if "framecrc" in args:
    for line in {stderr!r}:
        sys.stderr.write(line + "\\n")
    with open(args[-1], "w") as crc:
        crc.write("#tb 0: 1/1000\\n0, 0, 0, 40, 10, 0x0\\n0, 9000, 9000, 40, 10, 0x0\\n")
    print("lavfi.scd.score=30.0")
    print("lavfi.scd.time=4.0")
else:
    for i, arg in enumerate(args):
        if arg == "-y":
            open(args[i + 1], "wb").write(b"jpeg")
"""


def _fake_ffmpeg(
    tmp_path: Path, *, cuda_fails: bool, stderr: tuple[str, ...] = ()
) -> tuple[Path, Path]:
    log = tmp_path / "ffmpeg.log"
    script = tmp_path / "ffmpeg"
    script.write_text(_FAKE_FFMPEG.format(log=str(log), cuda_fails=cuda_fails, stderr=stderr))
    script.chmod(0o755)
    return script, log


def _media(script: Path, decode: str, seen: list[str] | None = None) -> frame_harvest.FfmpegMedia:
    return frame_harvest.FfmpegMedia(
        ffmpeg=str(script),
        ffprobe="true",
        decode=decode,
        on_fallback=None if seen is None else seen.append,
    )


def test_cuda_decode_passes_hwaccel_to_the_scan_only(tmp_path: Path) -> None:
    script, log = _fake_ffmpeg(tmp_path, cuda_fails=False)
    media = _media(script, "cuda")
    media._scdet(tmp_path / "a.mkv")
    assert media.frames(tmp_path / "a.mkv", [1000, 2000]) == [b"jpeg", b"jpeg"]
    scan_line, frames_line = log.read_text().splitlines()
    assert scan_line.count("-hwaccel cuda -i") == 1
    assert "-hwaccel" not in frames_line
    assert "hwaccel_output_format" not in log.read_text()
    assert media.decoder == "cuda"


def test_a_failed_cuda_scan_decodes_on_the_cpu_and_reports_ffmpegs_last_line(
    tmp_path: Path,
) -> None:
    script, log = _fake_ffmpeg(tmp_path, cuda_fails=True)
    seen: list[str] = []
    media = _media(script, "cuda", seen)
    cuts, last, _ = media._scdet(tmp_path / "a.mkv")
    assert [c.t_ms for c in cuts] == [4000]
    assert last == 9000
    assert seen == ["Device creation failed: -1."]
    assert media.decoder == "cpu"
    assert media.frames(tmp_path / "a.mkv", [1000]) == [b"jpeg"]
    assert log.read_text().count("-hwaccel") == 1, "after the fallback nothing asks for CUDA"


_DAMAGED = (
    "[hevc @ 0x1] The cu_qp_delta 53 is outside the valid range [-32, 31].",
    "[hevc @ 0x1] Skipping invalid undecodable NALU: 51",
    "[matroska,webm @ 0x2] Unknown profile bitstream",
)


def test_a_scan_that_exits_0_with_decoder_errors_is_a_damaged_source(tmp_path: Path) -> None:
    script, log = _fake_ffmpeg(tmp_path, cuda_fails=False, stderr=_DAMAGED)
    media = _media(script, "cpu")
    with pytest.raises(MediaError) as caught:
        media.scan(tmp_path / "a.mkv")
    assert caught.value.reason == "source file damaged (2 decode errors)"
    assert "cu_qp_delta 53" in str(caught.value)
    assert "-loglevel warning" in log.read_text()


def test_a_damaged_source_is_never_retried_on_the_other_decoder(tmp_path: Path) -> None:
    script, log = _fake_ffmpeg(tmp_path, cuda_fails=False, stderr=_DAMAGED)
    media = _media(script, "cuda")
    with pytest.raises(MediaError, match="damaged"):
        media._scdet(tmp_path / "a.mkv")
    assert len(log.read_text().splitlines()) == 1
    assert media.decoder == "cuda"


def test_only_the_first_five_error_lines_are_kept_in_the_message(tmp_path: Path) -> None:
    errors = tuple(f"[hevc @ 0x1] Invalid NAL unit {n}, skipping." for n in range(8))
    script, _ = _fake_ffmpeg(tmp_path, cuda_fails=False, stderr=errors)
    with pytest.raises(MediaError) as caught:
        _media(script, "cpu").scan(tmp_path / "a.mkv")
    assert caught.value.reason == "source file damaged (8 decode errors)"
    assert "NAL unit 4," in str(caught.value) and "NAL unit 5," not in str(caught.value)


def test_non_decoder_warnings_index_normally_and_are_counted(tmp_path: Path) -> None:
    warns = (
        "[matroska,webm @ 0x2] Unknown profile bitstream",
        "[aac @ 0x3] Invalid data in the audio stream",
        "Past duration 0.6 too large",
    )
    script, _ = _fake_ffmpeg(tmp_path, cuda_fails=False, stderr=warns)
    scan = _media(script, "cpu").scan(tmp_path / "a.mkv")
    assert scan.last_frame_ms == 9000
    assert scan.warnings == 3


def test_a_cuda_setup_notice_from_the_decoder_is_not_damage(tmp_path: Path) -> None:
    notice = "[hevc @ 0x1] Failed setup for format cuda: hwaccel initialisation returned error."
    script, _ = _fake_ffmpeg(tmp_path, cuda_fails=False, stderr=(notice,))
    scan = _media(script, "cuda").scan(tmp_path / "a.mkv")
    assert scan.warnings == 1


def test_a_damaged_title_writes_no_rows_and_records_its_reason(config: Config) -> None:
    media = FakeMedia()
    media.damaged_parts = {2}  # imdb:new
    result = run(config, media, encoder_with(), limit=2)
    assert (result.indexed, result.failed) == (1, 1)
    state = rows(config, "SELECT reason FROM frame_index_state WHERE item_id = 'imdb:new'")[0]
    assert state["reason"] == "source file damaged (3 decode errors)"
    assert rows(config, "SELECT count(*) FROM shots WHERE item_id = 'imdb:new'")[0][0] == 0
    assert any("decode_warnings=2" in line for line in result.logs)  # type: ignore[attr-defined]


def test_status_shows_the_decoder_and_the_fallback(
    config: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from plexdb.cli import main

    monkeypatch.setenv("PLEXDB_FRAME_DECODE", "cuda")
    shown: list[str] = []
    for _ in range(2):
        assert main(["frames", "status"]) == 0
        shown.append(json.loads(capsys.readouterr().out)["decoder"])
        frames.write_decoder_fallback(
            frames.frames_path(Config.from_env()), "Device creation failed: -1."
        )
    assert shown == ["cuda", "cpu (cuda requested: Device creation failed: -1.)"]
    media = frame_harvest.configured_media(Config.from_env())
    assert media.decoder == "cuda"
    assert not frames.decoder_path(frames.frames_path(config)).exists(), "a new run starts clean"


def test_a_row_with_no_lock_holder_reads_as_not_indexing(config: Config) -> None:
    path = frames_path(config)
    when = datetime(2026, 1, 1)
    kwargs: dict[str, Any] = {"hours": "", "encoder_reachable": None, "ffmpeg_found": False}
    with frames.open_frames(path) as conn:
        frames.set_indexing(conn, "imdb:new", now=when)
        assert not frames.harvester_running(path)
        assert frames.status(conn, {}, path=path, now=when, **kwargs)["indexing"] is None
        with frames.harvest_lock(path):
            assert frames.harvester_running(path)
            held = frames.status(conn, {}, path=path, now=when, **kwargs)["indexing"]
            assert held == {"item_id": "imdb:new", "since": when.isoformat()}


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs a real ffmpeg")
def test_a_time_past_the_last_frame_costs_only_its_own_jpeg(tmp_path: Path) -> None:
    video = tmp_path / "a.mp4"
    make = [
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=10:s=320x240:r=24",
        "-c:v", "libx264", "-g", "48", "-y", str(video),
    ]  # fmt: skip
    subprocess.run(make, check=True)
    media = frame_harvest.FfmpegMedia(ffmpeg="ffmpeg", ffprobe="ffprobe", decode="cpu")
    got = media.frames(video, [20000, 1000, 9990, 5000, 10500, 3000])
    assert [g is not None for g in got] == [False, True, False, True, False, True]
