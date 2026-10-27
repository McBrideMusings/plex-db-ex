"""The seeded damaged-files panel lists a failed check from `media_check` and from
`extra_check`, and leaves out a decode that ran past its time limit."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fixture_store import init

from plexdb.panels import DECODE_TIMED_OUT, DEFAULT_DASHBOARDS
from plexdb.store import open_store


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    store = tmp_path / "plexdb.db"
    init(store)
    with open_store(store) as connection:
        yield connection


def _damaged_sql() -> str:
    return str(
        next(
            p["sql"] for d in DEFAULT_DASHBOARDS for p in d["panels"] if p["id"] == "damaged-files"
        )
    )


def test_it_lists_a_bad_part_and_a_bad_extra_and_not_a_good_or_timed_out_one(
    conn: sqlite3.Connection,
) -> None:
    conn.execute("INSERT INTO items (item_id, type, title) VALUES ('t:1', 'movie', 'Movie')")
    conn.execute("INSERT INTO items (item_id, type, title) VALUES ('fs:1', 'extra', 'Bumper')")
    for part_id, name in ((1, "good"), (2, "bad"), (3, "slow")):
        conn.execute(
            "INSERT INTO media_part (part_id, item_id, rating_key, file, fetched_at)"
            " VALUES (?, 't:1', ?, ?, 'x')",
            (part_id, str(part_id), f"/lib/{name}.mp4"),
        )
    conn.execute("INSERT INTO extra_library VALUES ('L', '/x', 'bumper', NULL, 'x', NULL)")
    conn.execute("INSERT INTO extra_item VALUES ('fs:1', 'L', 1, NULL, 'x', NULL)")
    for name in ("good", "bad"):
        conn.execute(
            "INSERT INTO extra_file (path, item_id, primary_file, first_seen, last_seen)"
            " VALUES (?, 'fs:1', 1, 'x', 'x')",
            (f"/x/{name}.mp4",),
        )
    conn.executemany(
        "INSERT INTO media_check (part_id, kind, checked_at, ok, error) VALUES (?, ?, ?, ?, ?)",
        [
            (1, "header", "2026-10-01", 1, None),
            (2, "header", "2026-10-02", 0, "file is empty"),
            (3, "decode", "2026-10-03", 0, DECODE_TIMED_OUT),
        ],
    )
    conn.executemany(
        "INSERT INTO extra_check (path, kind, checked_at, ok, error) VALUES (?, ?, ?, ?, ?)",
        [
            ("/x/good.mp4", "header", "2026-10-04", 1, None),
            ("/x/bad.mp4", "header", "2026-10-05", 0, "does not parse: moov atom not found"),
        ],
    )

    rows = [tuple(r) for r in conn.execute(_damaged_sql())]

    assert rows == [
        ("Bumper", "/x/bad.mp4", "header", "does not parse: moov atom not found", "2026-10-05"),
        ("Movie", "/lib/bad.mp4", "header", "file is empty", "2026-10-02"),
    ]
