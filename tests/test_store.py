"""The busy timeout is explicit, not inherited (issue #16).

`sqlite3.connect` happens to default `timeout=5.0`, so a busy timeout was
already in effect before this file set `PRAGMA busy_timeout` itself — but
nothing stated that value or chose it. Under WAL a reader never contends with
a writer (that is the whole point of ADR-0007), so the case this timeout
actually governs is writer versus writer: two `plexdb` commands running at
once, or a sweep overlapping anything else that writes. These tests prove
that with real connections, not by reading the pragma back.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from plexdb.store import BUSY_TIMEOUT_MS, init, open_readonly, open_store


def test_busy_timeout_is_set_explicitly_on_open_store_and_open_readonly(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init(store)

    with open_store(store) as conn:
        write_value = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    with open_readonly(store) as conn:
        readonly_value = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    assert write_value == BUSY_TIMEOUT_MS
    assert readonly_value == BUSY_TIMEOUT_MS


def test_a_contended_write_waits_and_then_succeeds(tmp_path: Path) -> None:
    """Two writers both attempting BEGIN IMMEDIATE do contend. The second
    should block while the first holds its transaction open, then complete
    once the first commits — not fail immediately."""
    store = tmp_path / "plexdb.db"
    init(store)
    second_done = threading.Event()

    def second_writer() -> None:
        with open_store(store) as second:
            second.execute("BEGIN IMMEDIATE")
            second.execute("INSERT INTO items (item_id, type, title) VALUES ('b', 'movie', 'B')")
            second.commit()
        second_done.set()

    with open_store(store) as first:
        first.execute("BEGIN IMMEDIATE")
        first.execute("INSERT INTO items (item_id, type, title) VALUES ('a', 'movie', 'A')")

        thread = threading.Thread(target=second_writer)
        thread.start()
        time.sleep(0.3)
        assert not second_done.is_set(), (
            "second writer should still be blocked on the first writer's open transaction"
        )
        first.commit()

    thread.join(timeout=BUSY_TIMEOUT_MS / 1000 + 5)
    assert second_done.is_set(), "second writer never completed"

    with open_readonly(store) as conn:
        rows = conn.execute("SELECT item_id FROM items ORDER BY item_id").fetchall()
    assert [r["item_id"] for r in rows] == ["a", "b"]


def test_with_the_timeout_forced_to_zero_the_second_writer_fails_fast(tmp_path: Path) -> None:
    """Same contention, but with the second writer's timeout forced to zero:
    it must fail immediately with 'database is locked' rather than wait — the
    proof that the timeout, not something else, is what makes the difference
    in the test above."""
    store = tmp_path / "plexdb.db"
    init(store)

    with open_store(store) as first:
        first.execute("BEGIN IMMEDIATE")
        first.execute("INSERT INTO items (item_id, type, title) VALUES ('a', 'movie', 'A')")

        with open_store(store) as second:
            second.execute("PRAGMA busy_timeout = 0")
            start = time.monotonic()
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                second.execute("BEGIN IMMEDIATE")
            elapsed = time.monotonic() - start

        first.commit()

    assert elapsed < 0.5, "a zero busy_timeout should fail immediately, not wait"
