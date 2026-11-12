"""The Python writer and `plexdb-rs` share one writer lock (ADR-0001).

These run the real binary, which `admin test` builds and names in `PLEXDB_RS_BIN`
(`tools/with-rs.sh`).
"""

from __future__ import annotations

import fcntl
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fixture_store import init

from plexdb import rust
from plexdb.config import Config
from plexdb.store import holding
from plexdb.sweep import Impl, plan, run_sweep

STEP = "fold-list-decisions"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "plexdb.db"
    init(path)
    monkeypatch.setenv("PLEXDB_PATH", str(path))
    monkeypatch.delenv("PLEXDB_EXPLORE_SAVED_PATH", raising=False)
    monkeypatch.delenv("PLEXDB_SNAPSHOT_PATH", raising=False)
    monkeypatch.delenv(rust.HELD_STORE_VAR, raising=False)
    return path


def test_a_rust_step_waits_while_another_process_claims_the_store(store: Path) -> None:
    """`plexdb idle <command>` and the migration claim the store exclusive; a Rust
    step that started anyway would write under them."""
    binary = rust.binary(Config.from_env())
    with Path(f"{store}.write-lock").open("a") as claim:
        fcntl.flock(claim, fcntl.LOCK_EX)
        child = subprocess.Popen([binary, STEP], stdout=subprocess.PIPE, text=True)
        time.sleep(1.5)
        assert child.poll() is None, "plexdb-rs ran while the store was claimed"
        fcntl.flock(claim, fcntl.LOCK_UN)
    out, _ = child.communicate(timeout=30)
    assert child.returncode == 0
    assert out.startswith("list decisions: no file at")


def test_a_rust_step_run_under_this_processs_own_claim_does_not_wait_on_it(store: Path) -> None:
    """`plexdb idle fold-list-decisions` holds the store, then hands the step to
    `plexdb-rs`. Waiting on its parent's claim, the child would never finish."""
    result: list[int] = []
    with holding(store) as refused:
        assert refused is None
        worker = threading.Thread(target=lambda: result.append(rust.run([STEP])), daemon=True)
        worker.start()
        worker.join(timeout=30)
    assert result == [0], "plexdb-rs waited on the lock its parent holds"


def test_migrate_creating_a_store_under_this_processs_own_claim_does_not_wait_on_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`plexdb idle migrate` on a store not created yet: the child must recognise
    the claim on a path that does not exist, or it waits on its parent forever."""
    path = tmp_path / "new" / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(path))
    monkeypatch.delenv(rust.HELD_STORE_VAR, raising=False)
    result: list[int] = []
    with holding(path) as refused:
        assert refused is None
        worker = threading.Thread(target=lambda: result.append(rust.run(["migrate"])), daemon=True)
        worker.start()
        worker.join(timeout=30)
    assert result == [0], "plexdb-rs migrate waited on the lock its parent holds"
    assert path.exists()


@pytest.mark.parametrize(
    ("script", "status", "reported"),
    [
        (None, "skipped", "skipped: cannot run"),
        (
            "echo 'error: PLEXDB_X must be set' >&2; exit 78",
            "skipped",
            "skipped: PLEXDB_X must be set",
        ),
        (
            "echo 'error: the file is malformed' >&2; exit 1",
            "failed",
            "failed, continuing: the file is malformed",
        ),
    ],
    ids=["missing", "not-configured", "failed"],
)
def test_a_broken_plexdb_rs_costs_only_its_own_steps(
    store: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    script: str | None,
    status: str,
    reported: str,
) -> None:
    """The sweep plans a Rust step from `rust_steps.json`, so a missing or failing
    binary cannot stop the walk, the plays or `publish` (ADR-0014); it is reported
    under the step's own policy, as a Python step's error is."""
    binary = tmp_path / "plexdb-rs"
    if script is not None:
        binary.write_text(f"#!/bin/sh\n{script}\n")
        binary.chmod(0o755)
    monkeypatch.setenv("PLEXDB_RS_BIN", str(binary))

    rust_step = next(step for step in plan() if step.name == STEP)
    assert rust_step.impl is Impl.RUST

    assert run_sweep(steps=[rust_step]) == 0
    out = capsys.readouterr().out
    assert reported in out
    assert f"{status} 1" in out


def test_plexdb_explore_becomes_the_rust_server_and_stops_cleanly_on_ctrl_c(store: Path) -> None:
    """`plexdb explore` replaces itself with `plexdb-rs explore`: the process that answers is
    the one that was started, and SIGINT (Ctrl-C) ends it with exit 0, not a traceback."""
    import os
    import signal
    import sys
    import urllib.request

    child = subprocess.Popen(
        [sys.executable, "-m", "plexdb.cli", "explore", "--port", "0", "--store", str(store)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PLEXDB_PATH": str(store)},
    )
    try:
        assert child.stdout is not None
        line = child.stdout.readline()
        assert line.startswith("Plex DB-EX on http://127.0.0.1:"), line
        base = line.split()[3].rstrip("/")
        with urllib.request.urlopen(f"{base}/api/tags?kind=movie", timeout=30) as answer:
            assert answer.status == 200
        command = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(child.pid)], capture_output=True, text=True
        )
        assert command.stdout.strip().endswith("plexdb-rs"), command.stdout
        child.send_signal(signal.SIGINT)
        _, errors = child.communicate(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    assert child.returncode == 0, errors
    assert "Traceback" not in errors
