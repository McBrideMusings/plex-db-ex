"""The Plex TVX Roles tab's server side, `role_decisions.json`, and the fold into
`keyword_role_decisions`."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from plexdb.cli import main
from plexdb.errors import RoleDecisionsError
from plexdb.explore import make_server
from plexdb.roles import fold_role_decisions
from plexdb.store import init, open_store

STATED = "2026-10-01T00:00:00+00:00"

# (keyword, role, source, score, error)
ROWS = [
    ("tokyo", "region", "wikidata", None, None),  # source-stated
    ("tokyo", "region", "jev", 0.97, None),
    ("tokyo", "tone", "jev", 0.10, None),
    ("bleak", "tone", "jev", 0.88, None),
    ("grief", "theme", "jev", 0.91, None),
    ("odd", "tone", "jev", None, "Jev returned 400"),  # a refusal
]


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "plexdb.db"
    init(path)
    with open_store(path) as conn:
        for keyword, role, source, score, error in ROWS:
            conn.execute(
                "INSERT INTO keyword_roles (keyword, role, source, score, model, error, stated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    keyword,
                    role,
                    source,
                    score,
                    "jev-1" if score is not None else None,
                    error,
                    STATED,
                ),
            )
        conn.execute(
            "INSERT INTO keyword_role_decisions VALUES ('grief', 'theme', 'rejected', ?)", (STATED,)
        )
        conn.execute("INSERT INTO keyword_forms (surface, keyword) VALUES ('Tokyo', 'tokyo')")
        conn.commit()
    return path


@pytest.fixture
def base_url(store: Path) -> Iterator[str]:
    server = make_server(store, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _send(method: str, url: str, body: object | None = None) -> tuple[int, dict[str, object]]:
    data = None if body is None else json.dumps(body).encode()
    headers = {} if data is None else {"Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def _decisions(store: Path) -> dict[tuple[str, str], tuple[str, str]]:
    with open_store(store) as conn:
        return {
            (k, r): (d, at) for k, r, d, at in conn.execute("SELECT * FROM keyword_role_decisions")
        }


def test_the_listing_shows_every_verdict_per_role_and_leaves_refusals_out(base_url: str) -> None:
    status, body = _send("GET", f"{base_url}/api/roles")

    assert status == 200
    assert body["stored"] == 3 and body["total"] == 3
    keywords = {k["keyword"]: k for k in body["keywords"]}  # type: ignore[attr-defined]
    assert list(keywords) == ["bleak", "grief", "tokyo"]
    tokyo = keywords["tokyo"]
    assert tokyo["form"] == "Tokyo"
    assert [s["source"] for s in tokyo["cells"]["region"]["sources"]] == ["wikidata", "jev"]
    assert tokyo["cells"]["region"]["sources"][0]["score"] is None
    assert set(tokyo["cells"]) == {"region", "tone"}
    assert keywords["grief"]["cells"]["theme"]["decision"] == "rejected"
    assert [(d["keyword"], d["role"]) for d in body["decided"]] == [("grief", "theme")]  # type: ignore[attr-defined]


def test_the_filter_matches_the_readable_or_stored_form(base_url: str) -> None:
    _, body = _send("GET", f"{base_url}/api/roles?q=TOK")
    assert [k["keyword"] for k in body["keywords"]] == ["tokyo"]  # type: ignore[attr-defined]
    assert (body["stored"], body["total"]) == (3, 1)


def test_a_decision_round_trips_marked_pending_and_leaves_the_store_alone(
    base_url: str, store: Path
) -> None:
    url = f"{base_url}/api/roles"

    status, body = _send("POST", url, {"keyword": "bleak", "role": "tone", "decision": "accepted"})

    assert status == 200
    assert body["cell"]["decision"] == "accepted" and body["cell"]["pending"] is True  # type: ignore[index]
    entries = json.loads((store.parent / "role_decisions.json").read_text())
    assert [(e["keyword"], e["role"], e["decision"]) for e in entries] == [
        ("bleak", "tone", "accepted")
    ]
    _, listing = _send("GET", url)
    cell = listing["keywords"][0]["cells"]["tone"]  # type: ignore[index]
    assert (cell["decision"], cell["pending"]) == ("accepted", True)
    assert listing["decided"][0]["keyword"] == "bleak"  # type: ignore[index]
    assert ("bleak", "tone") not in _decisions(store)


def test_clearing_a_folded_decision_is_pending_until_the_next_fold(base_url: str) -> None:
    url = f"{base_url}/api/roles"
    _, body = _send("POST", url, {"keyword": "grief", "role": "theme", "decision": "cleared"})

    assert (body["cell"]["decision"], body["cell"]["pending"]) == (None, True)  # type: ignore[index]
    assert _send("GET", url)[1]["decided"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"keyword": "bleak", "role": "mood", "decision": "accepted"},
        {"keyword": "bleak", "role": "tone", "decision": "maybe"},
        {"keyword": "", "role": "tone", "decision": "accepted"},
        {"keyword": "x" * 201, "role": "tone", "decision": "accepted"},
    ],
)
def test_a_malformed_decision_is_a_400_and_writes_nothing(
    base_url: str, store: Path, body: dict[str, object]
) -> None:
    assert _send("POST", f"{base_url}/api/roles", body)[0] == 400
    assert not (store.parent / "role_decisions.json").exists()


@pytest.mark.parametrize(
    ("keyword", "role"),
    [("nowhere", "region"), ("bleak", "era"), ("odd", "tone")],
)
def test_a_decision_for_a_keyword_role_with_no_verdict_is_a_404_and_writes_nothing(
    base_url: str, store: Path, keyword: str, role: str
) -> None:
    status, _ = _send(
        "POST", f"{base_url}/api/roles", {"keyword": keyword, "role": role, "decision": "accepted"}
    )
    assert status == 404
    assert not (store.parent / "role_decisions.json").exists()


def test_the_file_the_page_wrote_folds_into_the_table(base_url: str, store: Path) -> None:
    url = f"{base_url}/api/roles"
    _send("POST", url, {"keyword": "bleak", "role": "tone", "decision": "accepted"})
    _send("POST", url, {"keyword": "tokyo", "role": "tone", "decision": "rejected"})
    _send("POST", url, {"keyword": "grief", "role": "theme", "decision": "cleared"})
    entries = json.loads((store.parent / "role_decisions.json").read_text())

    with open_store(store) as conn:
        stats = fold_role_decisions(conn, store.parent / "role_decisions.json")

    assert (stats.entries, stats.matched, stats.unmatched) == (3, 3, 0)
    decided_at = {(e["keyword"], e["role"]): e["decided_at"] for e in entries}
    assert _decisions(store) == {
        ("bleak", "tone"): ("accepted", decided_at[("bleak", "tone")]),
        ("tokyo", "tone"): ("rejected", decided_at[("tokyo", "tone")]),
    }
    _, listing = _send("GET", url)
    assert all(not d["pending"] for d in listing["decided"])  # type: ignore[attr-defined]


def _entry(keyword: str, role: str, decision: str, at: str = STATED) -> dict[str, str]:
    return {"keyword": keyword, "role": role, "decision": decision, "decided_at": at}


def test_folding_applies_in_order_and_counts_unmatched(tmp_path: Path, store: Path) -> None:
    path = tmp_path / "role_decisions.json"
    path.write_text(
        json.dumps(
            [
                _entry("bleak", "tone", "rejected"),
                _entry("bleak", "tone", "accepted"),
                _entry("tokyo", "tone", "accepted"),
                _entry("tokyo", "tone", "cleared"),
                _entry("odd", "tone", "accepted"),
                _entry("nowhere", "era", "accepted"),
            ]
        )
    )

    with open_store(store) as conn:
        stats = fold_role_decisions(conn, path)

    assert (stats.entries, stats.matched, stats.unmatched) == (6, 4, 2)
    assert _decisions(store) == {
        ("bleak", "tone"): ("accepted", STATED),
        ("grief", "theme"): ("rejected", STATED),
    }


@pytest.mark.parametrize(
    "entries",
    [
        "{not json",
        {"not": "a list"},
        [_entry("bleak", "tone", "maybe")],
        [_entry("bleak", "mood", "accepted")],
        [_entry("", "tone", "accepted")],
        [_entry("bleak", "tone", "accepted", at="yesterday")],
        [{"keyword": "bleak", "role": "tone", "decision": "accepted"}],
    ],
)
def test_an_invalid_file_is_refused_whole_and_changes_nothing(
    tmp_path: Path, store: Path, entries: object
) -> None:
    path = tmp_path / "role_decisions.json"
    if isinstance(entries, list):
        entries = [_entry("bleak", "tone", "accepted"), *entries]
    path.write_text(entries if isinstance(entries, str) else json.dumps(entries))
    before = _decisions(store)

    with open_store(store) as conn, pytest.raises(RoleDecisionsError):
        fold_role_decisions(conn, path)

    assert _decisions(store) == before


def test_a_missing_file_is_no_decisions(tmp_path: Path, store: Path) -> None:
    with open_store(store) as conn:
        stats = fold_role_decisions(conn, tmp_path / "role_decisions.json")
    assert (stats.file_found, stats.entries) == (False, 0)
    assert _decisions(store) == {("grief", "theme"): ("rejected", STATED)}


def test_the_command_folds_the_file_beside_the_saved_queries_file(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("PLEXDB_SNAPSHOT_PATH", "")
    monkeypatch.setenv("PLEXDB_EXPLORE_SAVED_PATH", "")
    (store.parent / "role_decisions.json").write_text(
        json.dumps([_entry("bleak", "tone", "accepted")])
    )

    assert main(["fold-role-decisions"]) == 0

    assert "1 matched a keyword role" in capsys.readouterr().out
    assert _decisions(store)[("bleak", "tone")] == ("accepted", STATED)


def test_an_empty_store_lists_nothing(tmp_path: Path) -> None:
    path = tmp_path / "plexdb.db"
    init(path)
    server = make_server(path, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _, body = _send("GET", f"http://127.0.0.1:{server.server_port}/api/roles")
    finally:
        server.shutdown()
        server.server_close()
    assert (body["stored"], body["keywords"], body["decided"]) == (0, [], [])


def test_a_decision_whose_verdict_is_gone_is_not_listed(base_url: str, store: Path) -> None:
    (store.parent / "role_decisions.json").write_text(
        json.dumps([_entry("vanished", "tone", "accepted")])
    )

    _, body = _send("GET", f"{base_url}/api/roles")

    assert [(d["keyword"], d["role"]) for d in body["decided"]] == [("grief", "theme")]  # type: ignore[attr-defined]
