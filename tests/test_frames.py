"""The Frame index file, frames.db, and its operator surface (ADR-0024)."""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from plexdb import frames
from plexdb.cli import main
from plexdb.store import init, open_store


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store with one movie in section 1 and one episode in section 2, and an
    empty frames directory. Returns the frames directory."""
    store = tmp_path / "plexdb.db"
    init(store)
    with open_store(store) as conn:
        for item_id, kind, section in (("imdb:tt1", "movie", "1"), ("tvdb:9", "episode", "2")):
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
    frames_dir = tmp_path / "frames"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("PLEXDB_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("PLEXDB_FRAMES_DIR", str(frames_dir))
    monkeypatch.setenv("PLEXDB_FRAME_HOURS", "")
    monkeypatch.setenv("PLEXDB_FRAME_ENCODER_URL", "")
    monkeypatch.setenv("PLEXDB_FRAME_MODEL", "clip-test")
    return frames_dir


class _Encoder(BaseHTTPRequestHandler):
    """An OpenAI-compatible embeddings server returning fixed 768-wide vectors,
    holding its first answer for `server.delay_s` the way a broker holds a request
    through a cold start."""

    server: _EncoderServer

    def do_GET(self) -> None:
        self._send(200, {"object": "list", "data": [{"id": "clip-test"}]})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        if len(self.server.requests) == 1:
            time.sleep(self.server.delay_s)
        data = [
            {"object": "embedding", "index": i, "embedding": [3.0] * frames.FRAME_DIMS}
            for i in range(len(body["input"]))
        ]
        self._send(200, {"object": "list", "model": body["model"], "data": data})

    def _send(self, status: int, payload: object) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args: object) -> None:
        pass


class _EncoderServer(ThreadingHTTPServer):
    requests: list[dict[str, Any]]
    delay_s: float


@pytest.fixture
def encoder() -> Iterator[_EncoderServer]:
    server = _EncoderServer(("127.0.0.1", 0), _Encoder)
    server.requests, server.delay_s = [], 0.0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _refused_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def test_status_on_a_fresh_directory_creates_one_current_file_with_every_count_zero(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for _ in range(2):
        assert main(["frames", "status"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["schema_version"] == frames.SCHEMA_VERSION
        assert report["sections"] == [
            {"section_id": "1", "indexed": 0, "total": 1},
            {"section_id": "2", "indexed": 0, "total": 1},
        ]
        assert report["queued"] == 0
        assert report["failed"] == []
        assert report["inside_hours"] is False
        assert report["indexing"] is None
        assert report["encoder_reachable"] is None
        assert report["bytes"] == (env / "frames.db").stat().st_size

    assert sorted(p.name for p in env.iterdir()) == ["frames.db"], "no -wal or -shm sidecar"


def test_status_reports_whether_the_configured_encoder_answers(
    env: Path,
    encoder: _EncoderServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for url, expected in (
        (f"http://127.0.0.1:{encoder.server_port}", True),
        (_refused_url(), False),
    ):
        monkeypatch.setenv("PLEXDB_FRAME_ENCODER_URL", url)
        assert main(["frames", "status"]) == 0
        assert json.loads(capsys.readouterr().out)["encoder_reachable"] is expected
    assert encoder.requests == [], "status never asks for an embedding"


def test_probe_encoder_waits_out_a_slow_first_answer_and_reports_equal_widths(
    env: Path,
    encoder: _EncoderServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    encoder.delay_s = 5.0
    monkeypatch.setenv("PLEXDB_FRAME_ENCODER_URL", f"http://127.0.0.1:{encoder.server_port}/")
    assert main(["frames", "probe-encoder"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["model"] == "clip-test"
    assert report["image"]["dims"] == report["text"]["dims"] == frames.FRAME_DIMS
    assert report["cold_start_wait_ms"] >= 4000, "the first, held answer is the cold start"

    cold, image, text = encoder.requests
    assert image["modality"] == "image"
    assert image["input"][0].startswith("data:image/jpeg;base64,/9j/"), "a JPEG data URI"
    for phrase in (cold, text):
        assert (phrase["modality"], phrase["input"]) == ("text", ["a red square"])


def test_probe_encoder_against_a_refused_url_exits_non_zero_naming_it(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url = _refused_url()
    monkeypatch.setenv("PLEXDB_FRAME_ENCODER_URL", url)
    assert main(["frames", "probe-encoder"]) == 1
    assert f"error: encoder unavailable at {url}/v1/embeddings" in capsys.readouterr().err

    monkeypatch.setenv("PLEXDB_FRAME_ENCODER_URL", "")
    assert main(["frames", "probe-encoder"]) == 1
    assert "PLEXDB_FRAME_ENCODER_URL is not set" in capsys.readouterr().err


def test_queue_adds_a_known_title_and_refuses_an_unknown_one(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["frames", "queue", "imdb:tt1"]) == 0
    assert main(["frames", "queue", "imdb:tt404"]) == 1
    assert "imdb:tt404 is not in items" in capsys.readouterr().err

    with frames.open_frames(env / "frames.db") as conn:
        rows = conn.execute("SELECT item_id, queued_by FROM frame_queue").fetchall()
    assert [tuple(row) for row in rows] == [("imdb:tt1", "cli")]


def test_check_fails_on_a_damaged_or_behind_frames_db_and_not_on_a_missing_one(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check"]) == 0
    assert "frames.db: none yet at" in capsys.readouterr().out

    path = env / "frames.db"
    frames.migrate(path)
    assert main(["check"]) == 0
    assert "frames.db quick_check: ok" in capsys.readouterr().out

    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_version SET version = 0")
    assert main(["check"]) == 1
    assert "frames.db FAILED: at v0" in capsys.readouterr().out

    path.unlink()
    frames.migrate(path)
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    assert main(["check"]) == 1
    assert "frames.db FAILED: " in capsys.readouterr().out


def test_prune_deletes_every_row_of_a_title_no_longer_in_items_and_keeps_the_rest(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vector = json.dumps([1] * frames.FRAME_DIMS)
    with frames.open_frames(env / "frames.db") as conn:
        for item_id in ("imdb:tt1", "imdb:gone"):
            conn.execute(
                "INSERT INTO frame_index_state (item_id, indexed_at) VALUES (?, 'now')", (item_id,)
            )
            conn.execute(
                "INSERT INTO cuts (item_id, t_ms, score, source) VALUES (?, 1000, 0.5, 'scdet')",
                (item_id,),
            )
            conn.execute(
                "INSERT INTO frame_queue (item_id, queued_at, queued_by) VALUES (?, 'now', 'cli')",
                (item_id,),
            )
            for t_ms in (0, 2000):
                conn.execute(
                    "INSERT INTO frame_vec (item_id, t_ms, shot_ix, vec) "
                    "VALUES (?, ?, 0, vec_int8(?))",
                    (item_id, t_ms, vector),
                )
        conn.commit()

    assert main(["prune-frames"]) == 0
    assert "pruned 1 title(s)" in capsys.readouterr().out

    with frames.open_frames(env / "frames.db") as conn:
        for table in ("frame_index_state", "cuts", "frame_queue", "frame_vec"):
            held = conn.execute(f"SELECT item_id, count(*) FROM {table} GROUP BY item_id")
            expected = 2 if table == "frame_vec" else 1
            assert [tuple(row) for row in held] == [("imdb:tt1", expected)], table

    with open_store(env.parent / "plexdb.db") as conn:
        conn.execute("DELETE FROM items")
        conn.commit()
    assert main(["prune-frames"]) == 1, "an empty items table is not every title removed"
    with frames.open_frames(env / "frames.db") as conn:
        assert conn.execute("SELECT count(*) FROM frame_vec").fetchone()[0] == 2


def test_prune_deletes_vectors_of_a_title_with_no_state_cuts_or_queue_row(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vector = json.dumps([1] * frames.FRAME_DIMS)
    with frames.open_frames(env / "frames.db") as conn:
        # 1100 vectors is more than one 1024-vector chunk.
        for item_id, count in (("imdb:tt1", 3), ("imdb:gone", 1100)):
            for t_ms in range(count):
                conn.execute(
                    "INSERT INTO frame_vec (item_id, t_ms, shot_ix, vec) "
                    "VALUES (?, ?, 0, vec_int8(?))",
                    (item_id, t_ms, vector),
                )
        conn.commit()

    assert main(["prune-frames"]) == 0
    assert "pruned 1 title(s)" in capsys.readouterr().out

    with frames.open_frames(env / "frames.db") as conn:
        held = conn.execute("SELECT item_id, count(*) FROM frame_vec GROUP BY item_id")
        assert [tuple(row) for row in held] == [("imdb:tt1", 3)]
        assert conn.execute("SELECT count(*) FROM frame_vec_rowids").fetchone()[0] == 3


def test_check_wait_runs_the_frames_quick_check_once_however_many_polls_it_makes(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from plexdb import store
    from plexdb.commands import check

    path = env / "frames.db"
    frames.migrate(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_version SET version = 0")

    checks = 0
    real = store.quick_check

    def counting(conn: sqlite3.Connection) -> str:
        nonlocal checks
        if str(conn.execute("PRAGMA database_list").fetchone()[2]).endswith("frames.db"):
            checks += 1
        return real(conn)

    now = 0.0

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr(store, "quick_check", counting)
    monkeypatch.setattr(check, "POLL_SECONDS", 5.0)
    real_await = check.await_current
    monkeypatch.setattr(
        check,
        "await_current",
        lambda look, wait: real_await(look, wait, sleep=sleep, clock=lambda: now),
    )

    assert main(["check", "--wait", "30"]) == 1
    out = capsys.readouterr().out
    assert out.count("frames.db is behind this build; waiting") >= 5
    assert checks == 1
    assert "frames.db FAILED: at v0" in out
