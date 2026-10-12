"""Display names (ADR-0021): `name_decisions.json`, the fold into `keyword_names`, the
labels Plex TVX shows, and that nothing that computes reads a name."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from plexdb.errors import NameDecisionsError
from plexdb.explore import make_server
from plexdb.keywords import readable_surfaces
from plexdb.names import NameDecisions, display_labels, fold_name_decisions, names_json
from plexdb.store import init, open_readonly, open_store

AT = "2026-10-01T00:00:00+00:00"
LATER = "2026-10-02T00:00:00+00:00"

# (item_id, key, value): heist is on two titles and bank robbery on one, so a merge of the
# two folds into heist; age gap is carried only as a spoiler keyword.
CARRIED = [
    ("imdb:tt1", "keyword", "heist"),
    ("imdb:tt2", "keyword", "heist"),
    ("imdb:tt1", "keyword", "bank robbery"),
    ("imdb:tt1", "spoiler_keyword", "age gap"),
]
FORMS = [
    ("Heists", "heist"),
    ("heist", "heist"),
    ("Bank Robbery", "bank robbery"),
    ("Age Gap", "age gap"),
]


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "plexdb.db"
    init(path)
    with open_store(path) as conn:
        for item_id in ("imdb:tt1", "imdb:tt2"):
            conn.execute(
                "INSERT INTO items (item_id, type, title) VALUES (?, 'movie', 'T')", (item_id,)
            )
        for item_id, key, value in CARRIED:
            conn.execute(
                "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
                "VALUES (?, 'keywords', 'tmdb', ?, ?, ?)",
                (item_id, key, value, AT),
            )
        conn.executemany("INSERT INTO keyword_forms (surface, keyword) VALUES (?, ?)", FORMS)
        conn.execute(
            "INSERT INTO keyword_surfaces (item_id, source, surface, rank, spoiler, fetched_at) "
            "VALUES ('imdb:tt2', 'tmdb', 'Heists', 1, 0, ?)",
            (AT,),
        )
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


def _send(method: str, url: str, body: object | None = None) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body).encode()
    headers = {} if data is None else {"Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def _merge(store: Path) -> None:
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO keyword_pairs (keyword_a, keyword_b, decision, decided_at) "
            "VALUES ('bank robbery', 'heist', 'accepted', ?)",
            (AT,),
        )
        conn.commit()


def _entry(surface: str, name: str | None, at: str = AT) -> dict[str, str]:
    if name is None:
        return {"surface": surface, "decision": "cleared", "decided_at": at}
    return {"surface": surface, "decision": "named", "name": name, "decided_at": at}


def _fold(store: Path, tmp_path: Path, entries: list[dict[str, str]]):  # type: ignore[no-untyped-def]
    path = tmp_path / "name_decisions.json"
    path.write_text(json.dumps(entries))
    with open_store(store) as conn:
        stats = fold_name_decisions(conn, path)
        rows = conn.execute("SELECT keyword, name FROM keyword_names ORDER BY 1").fetchall()
    return stats, [tuple(r) for r in rows]


def test_folding_writes_clears_and_counts_a_keyword_no_title_carries(
    store: Path, tmp_path: Path
) -> None:
    stats, rows = _fold(
        store,
        tmp_path,
        [
            _entry("Heists", "Heist"),
            _entry("Bank Robbery", "Robbery"),
            _entry("Bank Robbery", None, LATER),
            _entry("Age Gap", "May–December"),
            _entry("zeppelins", "Airships"),
        ],
    )
    assert rows == [("age gap", "May–December"), ("heist", "Heist")]
    assert (stats.entries, stats.matched, stats.unmatched) == (5, 4, 1)


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ({"surface": "heist", "decision": "named", "decided_at": AT}, "entry 1 needs text name"),
        (_entry("heist", "   "), "entry 1 has a blank name"),
        (_entry("heist", "x" * 201), "entry 1 has a name over 200 characters"),
    ],
)
def test_a_named_entry_without_a_usable_name_refuses_the_whole_file(
    store: Path, tmp_path: Path, bad: dict[str, str], message: str
) -> None:
    path = tmp_path / "name_decisions.json"
    path.write_text(json.dumps([_entry("Heists", "Heist"), bad]))
    with open_store(store) as conn, pytest.raises(NameDecisionsError, match=message):
        fold_name_decisions(conn, path)
    with open_readonly(store) as conn:
        assert conn.execute("SELECT COUNT(*) FROM keyword_names").fetchone()[0] == 0


def test_a_label_is_the_name_else_the_readable_spelling_and_the_file_overrides_the_store(
    store: Path, tmp_path: Path
) -> None:
    _fold(store, tmp_path, [_entry("Heists", "Heist")])
    overlay: dict[tuple[str, ...], dict[str, str]] = {
        ("bank robbery",): _entry("Bank Robbery", "Robbery"),
        ("heist",): _entry("heist", None),
    }
    with open_readonly(store) as conn:
        assert display_labels(conn, {})["heist"] == "Heist"
        labels = display_labels(conn, overlay)
        named = names_json(conn, overlay)
    assert (labels["heist"], labels["bank robbery"]) == ("heist", "Robbery")
    assert named["heist"] == {"name": None, "decided_at": AT, "pending": True}
    assert named["bank robbery"]["pending"] is True


def test_a_merge_group_shows_a_members_name_when_its_head_has_none(
    store: Path, tmp_path: Path
) -> None:
    _merge(store)
    _fold(store, tmp_path, [_entry("Bank Robbery", "Bank job")])
    with open_readonly(store) as conn:
        assert display_labels(conn, {})["heist"] == "Bank job"
    _fold(store, tmp_path, [_entry("Bank Robbery", "Bank job"), _entry("heist", "Heist", LATER)])
    with open_readonly(store) as conn:
        assert display_labels(conn, {})["heist"] == "Heist"


def test_the_judges_never_read_a_name(store: Path, tmp_path: Path) -> None:
    _fold(store, tmp_path, [_entry("Heists", "Something else entirely")])
    with open_readonly(store) as conn:
        assert readable_surfaces(conn)["heist"] == "heist"


def test_a_name_round_trips_through_the_page_pending_and_leaves_the_store_alone(
    base_url: str, store: Path
) -> None:
    status, out = _send(
        "POST", f"{base_url}/api/names", {"keyword": "heist", "name": "  Heist   job "}
    )
    assert status == 200
    assert out["label"] == "Heist job"
    assert out["name"] == {
        "name": "Heist job",
        "decided_at": out["entry"]["decided_at"],
        "pending": True,
    }
    assert NameDecisions.resolve((out["entry"]["surface"],)) == ("heist",)
    _, doctor = _send("GET", f"{base_url}/api/doctor?item_id=imdb:tt2")
    assert doctor["forms"] == {"heist": "Heist job"}
    assert doctor["names"]["heist"]["pending"] is True
    _, card = _send("GET", f"{base_url}/api/title?item_id=imdb:tt2")
    assert card["keywords"] == [{"value": "heist", "label": "Heist job"}]
    with open_readonly(store) as conn:
        assert conn.execute("SELECT COUNT(*) FROM keyword_names").fetchone()[0] == 0


def test_a_blank_name_clears_and_a_keyword_no_title_carries_is_a_404(base_url: str) -> None:
    _send("POST", f"{base_url}/api/names", {"keyword": "heist", "name": "Heist job"})
    status, out = _send("POST", f"{base_url}/api/names", {"keyword": "heist", "name": "   "})
    assert (status, out["label"], out["entry"]["decision"]) == (200, "heist", "cleared")
    status, out = _send("POST", f"{base_url}/api/names", {"keyword": "zeppelin", "name": "Airship"})
    assert status == 404
    assert "zeppelin" in str(out["error"])
    status, out = _send("POST", f"{base_url}/api/names", {"keyword": "heist"})
    assert status == 400
    assert "name is required" in str(out["error"])
