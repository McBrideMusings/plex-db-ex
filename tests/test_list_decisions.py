"""Crowd-list decisions (plex-db-ex-k1q.5): `list_decisions.json`, the fold into
`list_decision`, the `list-decide` command, and how the list table and `plexdb check`
show a decision and a failed fetch."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from plexdb import cli
from plexdb.commands.check import render
from plexdb.errors import ListDecisionsError
from plexdb.health import inspect
from plexdb.list_decisions import (
    ListDecisions,
    UnknownList,
    canonical_list_url,
    decide,
    fold_list_decisions,
)
from plexdb.lists import list_table, with_pending
from plexdb.store import init, open_readonly, open_store

AT = "2026-10-01T00:00:00+00:00"
LATER = "2026-10-02T00:00:00+00:00"
MINE = "https://mdblist.com/lists/lina/top-watched"


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "plexdb.db"
    init(path)
    with open_store(path) as conn:
        conn.execute(
            "INSERT INTO collection (collection_id, source, name, url, size, likes, observed_at) "
            "VALUES ('mdblist:14', 'mdblist', 'Top Watched', ?, 5, 9, ?)",
            (MINE, AT),
        )
        conn.commit()
    return path


def _write(path: Path, *entries: tuple[str, str, str]) -> Path:
    path.write_text(
        json.dumps([{"url": u, "decision": d, "decided_at": t} for u, d, t in entries]),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "raw",
    [
        MINE,
        "https://www.mdblist.com/lists/Lina/Top-Watched/",
        "http://mdblist.com/lists/lina/top-watched?x=1",
    ],
)
def test_one_list_has_one_canonical_url(raw: str) -> None:
    assert canonical_list_url(raw) == MINE


@pytest.mark.parametrize(
    "raw",
    ["https://letterboxd.com/lists/a/b", "https://mdblist.com/lists/lina", "mdblist:14", ""],
)
def test_text_that_is_not_an_mdblist_list_has_no_canonical_url(raw: str) -> None:
    assert canonical_list_url(raw) is None


def test_the_last_decision_per_list_wins_and_the_file_keeps_one_entry_per_list(
    tmp_path: Path,
) -> None:
    decisions = ListDecisions(tmp_path / "list_decisions.json")
    decisions.record((MINE,), "excluded", {})
    entry = decisions.record((MINE,), "refetch", {})

    assert decisions.latest() == {(MINE,): entry}
    assert len(json.loads(decisions.path.read_text())) == 1


def test_a_file_naming_something_that_is_not_a_list_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path / "list_decisions.json", ("https://example.com/x", "added", AT))
    with pytest.raises(ListDecisionsError, match="not an MDBList or Letterboxd list"):
        ListDecisions.load(path)


def test_the_fold_keeps_a_lists_added_flag_through_a_refetch_and_ends_it_on_removal(
    store: Path, tmp_path: Path
) -> None:
    path = tmp_path / "list_decisions.json"
    with open_store(store) as conn:
        _write(path, (MINE, "added", AT))
        stats = fold_list_decisions(conn, path)
        assert (stats.entries, stats.matched, stats.unmatched) == (1, 1, 0)

        _write(path, (MINE, "refetch", LATER))
        fold_list_decisions(conn, path)
        assert tuple(conn.execute("SELECT decision, added FROM list_decision").fetchone()) == (
            "refetch",
            1,
        )

        _write(path, (MINE, "removed", LATER))
        fold_list_decisions(conn, path)
        assert tuple(conn.execute("SELECT decision, added FROM list_decision").fetchone()) == (
            "removed",
            0,
        )


def test_a_missing_file_is_no_decisions(store: Path, tmp_path: Path) -> None:
    with open_store(store) as conn:
        assert not fold_list_decisions(conn, tmp_path / "none.json").file_found


def test_deciding_a_list_the_store_never_heard_of_is_refused_except_to_add_or_exclude(
    store: Path, tmp_path: Path
) -> None:
    decisions = ListDecisions(tmp_path / "list_decisions.json")
    other = "https://mdblist.com/lists/someone/else"
    with open_readonly(store) as conn:
        for verdict in ("refetch", "retry", "removed"):
            with pytest.raises(UnknownList):
                decide(conn, decisions, other, verdict)
        assert decide(conn, decisions, other, "excluded")["decision"] == "excluded"
        assert decide(conn, decisions, MINE.upper(), "refetch")["url"] == MINE
        assert decide(conn, decisions, other, "refetch")["decision"] == "refetch"
        with pytest.raises(ValueError, match="MDBList list"):
            decide(conn, decisions, "https://example.com/x", "added")
        with pytest.raises(ValueError, match="decision must be one of"):
            decide(conn, decisions, MINE, "deleted")


def test_list_decide_writes_the_file_the_explorer_reads(
    store: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.delenv("PLEXDB_EXPLORE_SAVED_PATH", raising=False)

    assert cli.main(["list-decide", MINE, "excluded"]) == 0
    assert "excluded " + MINE in capsys.readouterr().out
    entries = json.loads((store.parent / "list_decisions.json").read_text())
    assert [(e["url"], e["decision"]) for e in entries] == [(MINE, "excluded")]

    assert cli.main(["list-decide", "https://mdblist.com/lists/no/one", "retry"]) == 1
    assert cli.main(["list-decide", "https://example.com/x", "added"]) == 1


def _attempt(conn: sqlite3.Connection, key: str, attempts: int) -> None:
    conn.execute(
        "INSERT INTO list_attempt (list_key, attempts, attempted_at, reason) "
        "VALUES (?, ?, ?, 'MDBList returned 503')",
        (key, attempts, LATER),
    )
    conn.commit()


def test_the_list_table_and_check_report_a_failed_list_and_a_stopped_one(store: Path) -> None:
    gone = "https://mdblist.com/lists/pierce/gone"
    with open_store(store) as conn:
        _attempt(conn, MINE, 1)
        _attempt(conn, gone, 3)
        conn.execute(
            "INSERT INTO list_decision (url, decision, decided_at, added) "
            "VALUES (?, 'added', ?, 1)",
            (gone, AT),
        )
        conn.commit()
        rows = {r["url"]: r for r in list_table(conn)}

    assert rows[MINE]["state"] == "failed" and rows[MINE]["failure"]["stopped"] is False
    assert rows[gone]["collection_id"] is None and rows[gone]["failure"]["stopped"] is True
    lines = render(inspect(store, store.parent / "backups"))
    assert "lists FAILED: 2 mdblist list(s) failed to fetch" in lines
    assert any(
        line.startswith(f"  {gone}: 3 failed fetch(es)") and "stopped until a retry" in line
        for line in lines
    )


def test_the_file_is_laid_over_the_table_until_the_sweep_acts_on_it(store: Path) -> None:
    new = "https://mdblist.com/lists/pierce/mine"
    with open_store(store) as conn:
        rows = list_table(conn)
    latest: dict[tuple[str, ...], dict[str, str]] = {
        (MINE,): {"url": MINE, "decision": "refetch", "decided_at": LATER},
        (new,): {"url": new, "decision": "added", "decided_at": LATER},
    }

    shown = {r["url"]: r for r in with_pending(rows, latest)}

    assert shown[MINE]["pending"] == "Waiting for sweep"
    assert shown[new]["pending"] == "Waiting for sweep" and shown[new]["collection_id"] is None
    done: dict[tuple[str, ...], dict[str, str]] = {
        (MINE,): {"url": MINE, "decision": "refetch", "decided_at": "2026-09-01T00:00:00+00:00"}
    }
    with open_store(store) as conn:
        assert with_pending(list_table(conn), done)[0]["pending"] is None
    excluded: dict[tuple[str, ...], dict[str, str]] = {
        (MINE,): {"url": MINE, "decision": "excluded", "decided_at": LATER}
    }
    with open_store(store) as conn:
        assert (
            with_pending(list_table(conn), excluded)[0]["pending"]
            == "Excluded · next sweep removes it"
        )


def test_a_refetch_cannot_replace_an_add_the_sweep_has_not_folded(
    store: Path, tmp_path: Path
) -> None:
    decisions = ListDecisions(tmp_path / "list_decisions.json")
    new = "https://mdblist.com/lists/pierce/new"
    with open_readonly(store) as conn:
        decide(conn, decisions, new, "added")
        with pytest.raises(ValueError, match="no sweep has folded the add"):
            decide(conn, decisions, new, "refetch")
    assert decisions.latest()[(new,)]["decision"] == "added"
