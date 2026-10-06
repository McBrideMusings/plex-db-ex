"""The `fetched` cursor helpers every enrichment writer shares."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from plexdb.cursors import load_fetched, upsert_fetched
from plexdb.store import init as init_store
from plexdb.store import open_store

TITLE = "imdb:tt1375666"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "plexdb.db"
    init_store(path)
    with open_store(path) as c:
        c.execute(
            "INSERT INTO items (item_id, type, title) VALUES (?, 'movie', 'Inception')", (TITLE,)
        )
        c.commit()
        yield c


def test_a_cursor_rolls_back_with_the_callers_transaction(conn: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError), conn:
        upsert_fetched(conn, "keywords", "tmdb", TITLE, "2026-10-06T00:00:00+00:00")
        raise RuntimeError("sweep interrupted after the cursor, before the rows")

    assert load_fetched(conn, "keywords", "tmdb") == {}


def test_a_cursor_is_read_back_only_under_its_own_source_and_key(
    conn: sqlite3.Connection,
) -> None:
    with conn:
        upsert_fetched(conn, "keywords", "tmdb", TITLE, "2026-01-01T00:00:00+00:00")
        upsert_fetched(conn, "keywords", "tmdb", TITLE, "2026-10-06T00:00:00+00:00")
        upsert_fetched(
            conn, "tmdb_edges", "tmdb", TITLE, "2026-02-02T00:00:00+00:00", "fetched_similar"
        )

    assert load_fetched(conn, "keywords", "tmdb") == {TITLE: "2026-10-06T00:00:00+00:00"}
    assert load_fetched(conn, "keywords", "anilist") == {}
    assert load_fetched(conn, "tmdb_edges", "tmdb") == {}
    assert load_fetched(conn, "tmdb_edges", "tmdb", "fetched_similar") == {
        TITLE: "2026-02-02T00:00:00+00:00"
    }
