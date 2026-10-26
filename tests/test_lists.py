"""Reading crowd lists back: the list table, one list's detail, and the commands
that print them.

Every figure asserted here is worked out at read time from what the harvest
stored (ADR-0012, ADR-0027); nothing is read from a stored score.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fixture_store import init as init_store

from plexdb import cli
from plexdb.health import inspect
from plexdb.lists import MIN_KEYWORD_TITLES, find_list, list_detail, list_table
from plexdb.store import open_store

NOW = "2026-01-01T00:00:00+00:00"


def _item(conn: sqlite3.Connection, item_id: str, title: str, year: int, **ids: str) -> None:
    conn.execute(
        "INSERT INTO items (item_id, type, title, year) VALUES (?, 'movie', ?, ?)",
        (item_id, title, year),
    )
    for ns, value in ids.items():
        conn.execute(
            "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
            "VALUES (?, ?, ?, 'movie', ?)",
            (item_id, ns, value, NOW),
        )


def _keyword(conn: sqlite3.Connection, item_id: str, keyword: str) -> None:
    conn.execute(
        "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
        "VALUES (?, 'keywords', 'tmdb', 'keyword', ?, ?)",
        (item_id, keyword, NOW),
    )


def _play(conn: sqlite3.Connection, item_id: str) -> None:
    conn.execute(
        "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) "
        "VALUES (?, ?, 1, 1700000000)",
        (f"h-{item_id}", item_id),
    )


def _list(
    conn: sqlite3.Connection,
    list_id: int,
    name: str,
    path: str,
    size: int | None,
    entries: list[tuple[str, str, str | None, int | None]],
    kind: str = "movie",
) -> None:
    """A fetched list as the harvest writes it (`crates/plexdb/src/collections.rs`): the
    collection row, every entry by position, and a membership for each entry the library owns."""
    collection_id = f"mdblist:{list_id}"
    conn.execute(
        "INSERT INTO collection (collection_id, source, name, url, size, likes, observed_at) "
        "VALUES (?, 'mdblist', ?, ?, ?, NULL, ?)",
        (collection_id, name, f"https://mdblist.com/lists/{path}", size, NOW),
    )
    for rank, (ns, value, title, year) in enumerate(entries, start=1):
        conn.execute(
            "INSERT INTO collection_entry (collection_id, rank, ns, value, kind, title, year) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (collection_id, rank, ns, value, kind, title, year),
        )
        owned = conn.execute(
            "SELECT item_id FROM external_ids WHERE ns = ? AND value = ? AND kind = ?",
            (ns, value, kind),
        ).fetchone()
        if owned is not None:
            conn.execute(
                "INSERT INTO collection_membership (collection_id, item_id, rank, observed_at) "
                "VALUES (?, ?, ?, ?)",
                (collection_id, owned[0], rank, NOW),
            )


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "plexdb.db"
    init_store(path)
    with open_store(path) as conn:
        for number in range(1, 6):
            _item(conn, f"imdb:tt{number}", f"Owned {number}", 2000 + number, imdb=f"tt{number}")
            _keyword(conn, f"imdb:tt{number}", "twist")
        for number in range(6, 40):
            _item(conn, f"imdb:tt{number}", f"Filler {number}", 1990, imdb=f"tt{number}")
        _item(conn, "imdb:tt100", "Common", 1980, imdb="tt100")
        for number in range(1, 4):
            _keyword(conn, f"imdb:tt{number}", "everywhere")
        for number in range(6, 40):
            _keyword(conn, f"imdb:tt{number}", "everywhere")
        _play(conn, "imdb:tt1")
        _play(conn, "imdb:tt2")
        _list(
            conn,
            3892,
            "Mindfuck Movies",
            "lina/mindfuck",
            7,
            [
                ("imdb", "tt1", "Owned 1", 2001),
                ("imdb", "tt9001", "Unowned One", 1999),
                ("imdb", "tt2", "Owned 2", 2002),
                ("imdb", "tt3", "Owned 3", 2003),
                ("imdb", "tt9002", "Unowned Two", 2010),
                ("imdb", "tt4", "Owned 4", 2004),
                ("imdb", "tt5", "Owned 5", 2005),
            ],
        )
        conn.commit()
    return path


def test_the_list_table_counts_owned_and_watched_entries_at_read_time(store: Path) -> None:
    with open_store(store) as conn:
        conn.row_factory = sqlite3.Row
        rows = list_table(conn)

    assert rows == [
        {
            "collection_id": "mdblist:3892",
            "source": "mdblist",
            "author": "lina",
            "name": "Mindfuck Movies",
            "url": "https://mdblist.com/lists/lina/mindfuck",
            "provenance": "feed",
            "seed_count": None,
            "entry_count": 7,
            "owned": 5,
            "size": 7,
            "watched": 2,
            "likes": None,
            "fetched_at": rows[0]["fetched_at"],
            "decision": None,
            "failure": None,
            "state": "ok",
        }
    ]
    with open_store(store) as conn:
        assert list_table(conn, source="letterboxd") == []


def test_a_title_walked_after_the_harvest_joins_its_list_without_a_rewrite(store: Path) -> None:
    with open_store(store) as conn:
        conn.row_factory = sqlite3.Row
        _item(conn, "imdb:tt9001", "Unowned One", 1999, imdb="tt9001")
        conn.commit()

        assert list_table(conn)[0]["owned"] == 6


def test_the_detail_lists_every_entry_in_rank_order_with_the_right_title(store: Path) -> None:
    with open_store(store) as conn:
        conn.row_factory = sqlite3.Row
        detail = list_detail(conn, "mdblist:3892")

    assert detail is not None
    rows = [(e["rank"], e["owned"], e["title"], e["year"]) for e in detail["entries"]]
    assert rows == [
        (1, True, "Owned 1", 2001),
        (2, False, "Unowned One", 1999),
        (3, True, "Owned 2", 2002),
        (4, True, "Owned 3", 2003),
        (5, False, "Unowned Two", 2010),
        (6, True, "Owned 4", 2004),
        (7, True, "Owned 5", 2005),
    ]
    assert [e["watched"] for e in detail["entries"]][:3] == [True, False, True]
    assert detail["entry_count"] == 7


def test_the_detail_names_a_keyword_the_list_carries_far_more_than_the_library(
    store: Path,
) -> None:
    with open_store(store) as conn:
        conn.row_factory = sqlite3.Row
        detail = list_detail(conn, "mdblist:3892")

    assert detail is not None
    assert detail["keyword_kind"] == "movie"
    assert detail["keyword_titles_in_list"] == 5
    assert detail["keyword_titles_in_library"] == 40
    # "twist" is on 5 of 5 list titles and 5 of 40 library titles: 1.0 / 0.125 = 8.
    # "everywhere" is on 3 of 5 and 37 of 40: below the lift threshold.
    assert detail["keywords"] == [{"keyword": "twist", "in_list": 5, "in_library": 5, "lift": 8.0}]
    assert MIN_KEYWORD_TITLES <= 5


def test_a_list_is_found_by_id_or_url_and_an_unknown_one_is_none(store: Path) -> None:
    with open_store(store) as conn:
        assert find_list(conn, "mdblist:3892") == "mdblist:3892"
        assert find_list(conn, "https://mdblist.com/lists/lina/mindfuck/") == "mdblist:3892"
        assert find_list(conn, "mdblist:1") is None
        assert list_detail(conn, "mdblist:1") is None


def _run(capfd: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    """Run a command; `capfd`, because `plexdb lists` is `plexdb-rs`, a child process that
    writes to the real stdout."""
    code = cli.main(list(argv))
    return code, capfd.readouterr().out


def test_the_commands_print_exactly_what_the_functions_return(
    store: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    with open_store(store) as conn:
        conn.row_factory = sqlite3.Row
        table: Any = list_table(conn)
        detail: Any = list_detail(conn, "mdblist:3892")

    code, out = _run(capfd, "lists", "--json")
    assert (code, json.loads(out)) == (0, table)
    code, out = _run(capfd, "list", "mdblist:3892", "--json")
    assert (code, json.loads(out)) == (0, detail)
    code, out = _run(capfd, "lists")
    assert code == 0 and "Mindfuck Movies" in out and "owned 5/7" in out
    code, out = _run(capfd, "list", "mdblist:3892")
    assert code == 0 and "Unowned One (1999)  [not owned]" in out


def test_check_reports_lists_per_source_with_entries_and_owned_share(store: Path) -> None:
    from plexdb.commands.check import render

    report = inspect(store, store.parent / "backups")

    assert "  mdblist: 1 list(s), 7 entries stored, 71.4% owned" in render(report)


def test_a_show_is_watched_when_any_of_its_episodes_has_a_play(tmp_path: Path) -> None:
    path = tmp_path / "plexdb.db"
    init_store(path)
    with open_store(path) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('tvdb:1', 'show', 'A Show')")
        conn.execute(
            "INSERT INTO items (item_id, type, title, show_item_id) "
            "VALUES ('plex:ep1', 'episode', 'Pilot', 'tvdb:1')"
        )
        conn.execute(
            "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
            "VALUES ('tvdb:1', 'tvdb', '1', 'show', ?)",
            (NOW,),
        )
        _play(conn, "plex:ep1")
        _list(conn, 7, "A List", "someone/a-list", None, [("tvdb", "1", None, None)], "show")
        conn.commit()

        assert list_table(conn)[0]["watched"] == 1
