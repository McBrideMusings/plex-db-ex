"""The Frame index file, frames.db, and its operator surface (ADR-0024)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

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
    return frames_dir


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
