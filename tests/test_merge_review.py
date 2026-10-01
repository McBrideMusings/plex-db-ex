"""The Plex TVX review page's server side: the queue, the mappings, and the decisions file."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from plexdb.explore import make_server
from plexdb.merge_review import MergeDecisions
from plexdb.store import init, open_store
from plexdb.synonyms import fold_merge_decisions

JUDGED = "2026-09-30T00:00:00+00:00"

# (keyword_a, keyword_b, jev_score, decision)
PAIRS = [
    ("heist", "robberi", 0.95, None),  # Jev-merged
    ("bank", "vault", 0.85, None),  # proposed
    ("cop", "polic", 0.60, None),  # proposed
    ("alien", "space", 0.50, None),  # proposed, at the lower edge
    ("cat", "dog", 0.49, None),  # below the band
    ("ghost", "spirit", 0.70, "accepted"),
    ("elf", "orc", 0.80, "rejected"),
    ("gun", "shot", 0.95, "rejected"),  # a rejection outranks a high score
]


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "plexdb.db"
    init(path)
    with open_store(path) as conn:
        for a, b, score, decision in PAIRS:
            conn.execute(
                "INSERT INTO keyword_pairs "
                "(keyword_a, keyword_b, jev_score, jev_model, judged_at, decision, decided_at) "
                "VALUES (?, ?, ?, 'jev-1', ?, ?, ?)",
                (a, b, score, JUDGED, decision, JUDGED if decision else None),
            )
        conn.execute(
            "INSERT INTO keyword_forms (surface, keyword) VALUES ('heists', 'heist'), "
            "('bank robbery', 'robberi')"
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


def _send(method: str, url: str, body: object | None = None) -> tuple[int, dict[str, object]]:
    data = None if body is None else json.dumps(body).encode()
    headers = {} if data is None else {"Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def _names(pairs: object) -> list[str]:
    assert isinstance(pairs, list)
    return [f"{p['keyword_a']}/{p['keyword_b']}" for p in pairs]


def test_the_queue_holds_exactly_the_undecided_pairs_scored_from_half_to_below_nine_tenths(
    base_url: str,
) -> None:
    status, body = _send("GET", f"{base_url}/api/merges")

    assert status == 200
    assert _names(body["proposed"]) == ["bank/vault", "cop/polic", "alien/space"]
    # A person's decisions first (equal times, best score first), then Jev's own. gun/shot
    # (0.95, rejected) is listed and is not merged: a decision outranks the score.
    assert _names(body["decided"]) == ["gun/shot", "elf/orc", "ghost/spirit", "heist/robberi"]
    merged = [p for p in body["decided"] if p["bucket"] == "merged"]  # type: ignore[attr-defined]
    assert sorted(_names(merged)) == ["ghost/spirit", "heist/robberi"]
    jev_pair = body["decided"][-1]  # type: ignore[index]
    assert (jev_pair["form_a"], jev_pair["form_b"]) == ("heists", "bank robbery")


def test_an_accept_writes_one_entry_and_moves_the_pair_out_of_the_queue(
    base_url: str, store: Path
) -> None:
    file = store.parent / "merge_decisions.json"

    status, body = _send(
        "POST",
        f"{base_url}/api/merges",
        {"keyword_a": "bank", "keyword_b": "vault", "decision": "accepted"},
    )

    assert status == 200
    assert body["pair"]["bucket"] == "merged" and body["pair"]["pending"] is True  # type: ignore[index]
    entries = json.loads(file.read_text())
    assert [(e["keyword_a"], e["keyword_b"], e["decision"]) for e in entries] == [
        ("bank", "vault", "accepted")
    ]
    _, listing = _send("GET", f"{base_url}/api/merges")
    assert _names(listing["proposed"]) == ["cop/polic", "alien/space"]
    assert "bank/vault" in _names(listing["decided"])
    # Plex TVX never wrote the store.
    with open_store(store) as conn:
        row = conn.execute("SELECT decision FROM keyword_pairs WHERE keyword_a = 'bank'").fetchone()
        assert row[0] is None


def test_an_undo_returns_the_pair_to_the_queue_and_the_file_keeps_one_entry_per_pair(
    base_url: str, store: Path
) -> None:
    url = f"{base_url}/api/merges"
    pair = {"keyword_a": "cop", "keyword_b": "polic"}
    _send("POST", url, {**pair, "decision": "rejected"})
    assert "cop/polic" not in _names(_send("GET", url)[1]["proposed"])

    _, undone = _send("POST", url, {**pair, "decision": "cleared"})

    assert undone["pair"]["bucket"] == "proposed"  # type: ignore[index]
    assert "cop/polic" in _names(_send("GET", url)[1]["proposed"])
    entries = json.loads((store.parent / "merge_decisions.json").read_text())
    assert [e["decision"] for e in entries] == ["cleared"]


def test_the_file_the_page_wrote_folds_into_the_table(base_url: str, store: Path) -> None:
    url = f"{base_url}/api/merges"
    _send("POST", url, {"keyword_a": "bank", "keyword_b": "vault", "decision": "accepted"})
    _send("POST", url, {"keyword_a": "ghost", "keyword_b": "spirit", "decision": "cleared"})

    with open_store(store) as conn:
        stats = fold_merge_decisions(conn, store.parent / "merge_decisions.json")
        rows = dict(
            ((a, b), d)
            for a, b, d in conn.execute("SELECT keyword_a, keyword_b, decision FROM keyword_pairs")
        )

    assert stats.matched == 2
    assert rows[("bank", "vault")] == "accepted"
    assert rows[("ghost", "spirit")] is None
    _, listing = _send("GET", url)
    assert all(not p["pending"] for p in listing["decided"])  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "body",
    [
        {"keyword_a": "vault", "keyword_b": "bank", "decision": "accepted"},
        {"keyword_a": "bank", "keyword_b": "vault", "decision": "maybe"},
        {"keyword_a": "bank", "keyword_b": 3, "decision": "accepted"},
        {"keyword_a": "bank", "keyword_b": "x" * 201, "decision": "accepted"},
    ],
)
def test_a_malformed_decision_is_a_400_and_writes_nothing(
    base_url: str, store: Path, body: dict[str, object]
) -> None:
    assert _send("POST", f"{base_url}/api/merges", body)[0] == 400
    assert not (store.parent / "merge_decisions.json").exists()


def test_a_decision_for_a_pair_the_table_never_judged_is_a_404_and_writes_nothing(
    base_url: str, store: Path
) -> None:
    status, _ = _send(
        "POST",
        f"{base_url}/api/merges",
        {"keyword_a": "aaa", "keyword_b": "bbb", "decision": "accepted"},
    )

    assert status == 404
    assert not (store.parent / "merge_decisions.json").exists()


def test_a_pair_jev_refused_is_not_reviewable_and_a_decision_for_it_is_a_404(
    base_url: str, store: Path
) -> None:
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_error, judged_at, decision, "
            "decided_at) VALUES ('odd', 'pair', 'Jev returned 400', ?, 'accepted', ?)",
            (JUDGED, JUDGED),
        )
        conn.commit()

    status, body = _send("GET", f"{base_url}/api/merges")
    assert status == 200
    assert "odd" not in json.dumps(body)

    status, _ = _send(
        "POST",
        f"{base_url}/api/merges",
        {"keyword_a": "odd", "keyword_b": "pair", "decision": "accepted"},
    )
    assert status == 404
    assert not (store.parent / "merge_decisions.json").exists()


def test_a_corrupt_decisions_file_is_refused_and_left_alone(base_url: str, store: Path) -> None:
    file = store.parent / "merge_decisions.json"
    file.write_text("{not json")

    assert _send("GET", f"{base_url}/api/merges")[0] == 500
    status, _ = _send(
        "POST",
        f"{base_url}/api/merges",
        {"keyword_a": "bank", "keyword_b": "vault", "decision": "accepted"},
    )

    assert status == 500
    assert file.read_text() == "{not json"


def test_a_cleared_entry_on_a_pair_below_the_band_lists_nothing_and_an_accept_lists_it(
    base_url: str,
) -> None:
    url = f"{base_url}/api/merges"
    pair = {"keyword_a": "cat", "keyword_b": "dog"}
    _send("POST", url, {**pair, "decision": "cleared"})
    assert "cat/dog" not in _names(_send("GET", url)[1]["decided"])

    _send("POST", url, {**pair, "decision": "accepted"})

    assert "cat/dog" in _names(_send("GET", url)[1]["decided"])


def test_the_file_refuses_a_decision_past_the_entry_cap_and_past_the_size_cap(
    tmp_path: Path,
) -> None:
    decisions = MergeDecisions(tmp_path / "merge_decisions.json")
    decisions.MAX_ENTRIES = 2
    decisions.record("a", "b", "accepted")
    decisions.record("c", "d", "accepted")
    with pytest.raises(ValueError, match="too many merge decisions"):
        decisions.record("e", "f", "accepted")
    assert len(decisions.latest()) == 2

    small = MergeDecisions(tmp_path / "small.json")
    small.MAX_TOTAL_BYTES = 100
    with pytest.raises(ValueError, match="exceed"):
        small.record("a", "b", "accepted")
    assert small.latest() == {}
