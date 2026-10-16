"""`plexdb search` hands `plexdb-read` the files and the options, and returns its answer."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from plexdb.cli import main


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake `plexdb-read` that prints its argv and standard input as JSON."""
    script = tmp_path / "plexdb-read"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}))\n"
        "sys.exit(int(__import__('os').environ.get('FAKE_EXIT', '0')))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PLEXDB_PATH", str(tmp_path / "plexdb.db"))
    monkeypatch.setenv("PLEXDB_SNAPSHOT_PATH", str(tmp_path / "snapshot.db"))
    monkeypatch.setenv("PLEXDB_FRAMES_DIR", str(tmp_path / "frames"))
    monkeypatch.setenv("PLEXDB_PHRASE_CACHE", "")
    monkeypatch.setenv("PLEXDB_FRAME_ENCODER_URL", "http://encoder.invalid:1")
    monkeypatch.setenv("PLEXDB_READ_BIN", str(script))
    return tmp_path


def test_search_passes_the_files_and_options_to_plexdb_read(
    env: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "search", "a cat",
            "--pool", "imdb:tt1", "imdb:tt2",
            "--not", "dog", "night",
            "--weight", "0.8", "--temperature", "0.2", "--draw", "3", "--seed", "9",
        ]
    )  # fmt: skip
    assert code == 0
    printed = json.loads(capfd.readouterr().out)
    assert printed["argv"] == [
        "--snapshot", str(env / "snapshot.db"),
        "--frames", str(env / "frames" / "frames.db"),
        "--phrase-cache", str(env / "frames" / "phrases.db"),
        "--encoder-url", "http://encoder.invalid:1",
        "search", "a cat", "--pool-file", "-",
        "--not", "dog", "night",
        "--weight", "0.8", "--temperature", "0.2", "--draw", "3", "--seed", "9",
    ]  # fmt: skip
    assert printed["stdin"] == "imdb:tt1\nimdb:tt2"


def test_search_returns_the_binarys_exit_code(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_EXIT", "1")
    assert main(["search", "cat", "--pool", "imdb:tt1"]) == 1


def test_search_names_what_is_missing(
    env: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PLEXDB_READ_BIN", str(env / "nowhere"))
    assert main(["search", "cat", "--pool", "imdb:tt1"]) == 1
    assert "cargo build -p plexdb-reader" in capfd.readouterr().err

    monkeypatch.setenv("PLEXDB_FRAME_ENCODER_URL", "")
    assert main(["search", "cat", "--pool", "imdb:tt1"]) == 1
    assert "PLEXDB_FRAME_ENCODER_URL" in capfd.readouterr().err
