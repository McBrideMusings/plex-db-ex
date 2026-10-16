"""The frame harvester: order, sampling, one transaction per title, and stopping."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from plexdb import frame_harvest, frames
from plexdb.config import Config
from plexdb.errors import MediaError
from plexdb.frame_encoder import EncoderClient
from plexdb.frame_harvest import Cut
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
                "Media": [{"duration": 10_000, "Part": [{"id": i + 1, "size": 1000 + i}]}],
            }
            for i, (item_id, _, section, added) in enumerate(TITLES)
            if section == section_key
        ]


class FakeMedia:
    """No decoding: every title has a cut at 3 s, so two Shots of 3 s and 7 s."""

    def __init__(self) -> None:
        self.hashes: dict[int, str] = {}
        self.fail_parts: set[int] = set()
        self.read: list[int] = []

    def file_hash(self, part_id: int, size: int) -> str:
        return self.hashes.get(part_id, f"h{part_id}")

    def cuts(self, part_id: int) -> list[Cut]:
        self.read.append(part_id)
        if part_id in self.fail_parts:
            raise MediaError(f"part {part_id}: ffmpeg scdet exited 1")
        return [Cut(3000, 22.5, "scdet"), Cut(5000, None, "chapter")]

    def frames(self, part_id: int, times_ms: Sequence[int]) -> list[bytes | None]:
        return [b"\xff\xd8jpeg" for _ in times_ms]


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
    run(config, FakeMedia(), encoder_with(), limit=1)
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
    # Shots 0-3 s (> 2 s): one frame 1 s in; 3-5 s: its midpoint; 5-10 s: 6 s and 8 s.
    vec = rows(config, "SELECT t_ms, shot_ix FROM frame_vec WHERE item_id = ? ORDER BY t_ms", item)
    assert [tuple(r) for r in vec] == [(1000, 0), (4000, 1), (6000, 2), (8000, 2)]
    state = rows(
        config,
        "SELECT shots, vectors, dims, quant, error, part_id, bytes FROM frame_index_state WHERE item_id = ?",  # noqa: E501
        item,
    )[0]
    assert (state["shots"], state["vectors"], state["dims"], state["quant"], state["error"]) == (
        3,
        len(vec),
        768,
        "int8",
        None,
    )


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
        config, "SELECT error, file_hash FROM frame_index_state WHERE item_id = 'imdb:new'"
    )[0]
    assert "scdet exited 1" in state["error"] and state["file_hash"] == "h2"
    assert any("item_id=imdb:new" in line and "error=" in line for line in result.logs)  # type: ignore[attr-defined]  # noqa: E501


def test_the_log_line_carries_the_fields_the_operator_reads(config: Config) -> None:
    result = run(config, FakeMedia(), encoder_with(), limit=1)
    (line,) = [row for row in result.logs if "item_id=imdb:new" in row]  # type: ignore[attr-defined]
    for field in (
        "frames: 2026-",
        "part=2",
        "bytes=1001",
        "decode_s=",
        "encode_s=",
        "shots=3",
        "vectors=4",
    ):
        assert field in line


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
        def cuts(self, part_id: int) -> list[Cut]:
            with frames.open_frames(frames_path(config)) as conn:
                seen.append(frames._indexing(conn))
            return super().cuts(part_id)

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


def test_a_stalled_or_unreadable_scdet_answer_fails_the_title_not_the_run() -> None:
    with pytest.raises(MediaError):
        frame_harvest.parse_scdet("lavfi.scd.score=high\n")
