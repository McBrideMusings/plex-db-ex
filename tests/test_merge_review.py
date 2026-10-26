"""The Plex DB-EX review page's server side: the queue, the mappings, and the decisions file."""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from fixture_store import fold, init

from plexdb.explore import make_server
from plexdb.keywords import FILM_SUFFIX_MODEL
from plexdb.merge_review import MergeDecisions, merge_map
from plexdb.store import open_store

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
    assert [(e["surface_a"], e["surface_b"], e["decision"]) for e in entries] == [
        ("bank", "vault", "accepted")
    ]
    _, listing = _send("GET", f"{base_url}/api/merges")
    assert _names(listing["proposed"]) == ["cop/polic", "alien/space"]
    assert "bank/vault" in _names(listing["decided"])
    # Plex DB-EX never wrote the store.
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

    stats = fold("fold-merge-decisions", store, store.parent / "merge_decisions.json")
    with open_store(store) as conn:
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


def _carry(store: Path, item_id: str, source: str, surface: str, keyword: str) -> None:
    """`item_id` carries `keyword`, which `source` sent as `surface`."""
    with open_store(store) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO items (item_id, type, title, year) VALUES (?, 'movie', ?, 2004)",
            (item_id, item_id.title()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
            "VALUES (?, 'keywords', ?, 'keyword', ?, ?)",
            (item_id, source, keyword, JUDGED),
        )
        conn.execute(
            "INSERT INTO keyword_surfaces (item_id, source, surface, spoiler, fetched_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (item_id, source, surface, JUDGED),
        )
        conn.execute(
            "INSERT OR IGNORE INTO keyword_forms (surface, keyword) VALUES (?, ?)",
            (surface, keyword),
        )
        conn.commit()


def test_a_decision_naming_a_keyword_no_title_carries_is_a_404_and_writes_nothing(
    base_url: str, store: Path
) -> None:
    _carry(store, "spin", "tmdb", "taekwondo", "taekwondo")
    status, _ = _send(
        "POST",
        f"{base_url}/api/merges",
        {"keyword_a": "tae kwon do", "keyword_b": "taekwondo", "decision": "accepted"},
    )

    assert status == 404
    assert not (store.parent / "merge_decisions.json").exists()


def test_a_merge_of_two_stored_keywords_no_judge_saw_lists_folds_and_clears(
    base_url: str, store: Path
) -> None:
    _carry(store, "spin", "tmdb", "taekwondo", "taekwondo")
    _carry(store, "kick", "tmdb", "tae kwon do", "tae kwon do")
    url = f"{base_url}/api/merges"
    pair = {"keyword_a": "tae kwon do", "keyword_b": "taekwondo"}

    status, body = _send("POST", url, {**pair, "decision": "accepted"})

    assert status == 200
    assert body["pair"]["score"] is None and body["pair"]["pending"] is True  # type: ignore[index]
    assert "tae kwon do/taekwondo" in _names(_send("GET", url)[1]["decided"])
    file = store.parent / "merge_decisions.json"
    assert fold("fold-merge-decisions", store, file).matched == 1
    with open_store(store) as conn:
        row = conn.execute(
            "SELECT judged_at, jev_score, decision FROM keyword_pairs WHERE keyword_b = 'taekwondo'"
        ).fetchone()
        assert tuple(row) == (None, None, "accepted")
        assert merge_map(conn)["taekwondo"] == "tae kwon do"
        # A row with neither a judge's verdict nor a person's decision is no row.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO keyword_pairs (keyword_a, keyword_b) VALUES ('p', 'q')")

    _send("POST", url, {**pair, "decision": "cleared"})
    assert fold("fold-merge-decisions", store, file).matched == 1
    with open_store(store) as conn:
        gone = conn.execute("SELECT 1 FROM keyword_pairs WHERE keyword_b = 'taekwondo'").fetchone()
        assert gone is None


def test_a_person_may_decide_a_pair_jev_refused_and_it_lists_with_no_score(
    base_url: str, store: Path
) -> None:
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_error, judged_at) "
            "VALUES ('odd', 'pair', 'Jev returned 400', ?)",
            (JUDGED,),
        )
        conn.commit()
    url = f"{base_url}/api/merges"
    assert "odd" not in json.dumps(_send("GET", url)[1])

    status, body = _send(
        "POST", url, {"keyword_a": "odd", "keyword_b": "pair", "decision": "accepted"}
    )

    assert status == 200
    assert body["pair"]["score"] is None and body["pair"]["bucket"] == "merged"  # type: ignore[index]
    assert "odd/pair" in _names(_send("GET", url)[1]["decided"])


def test_the_tag_doctor_shows_each_sources_spellings_stored_values_and_merges(
    base_url: str, store: Path
) -> None:
    _carry(store, "spin", "tmdb", "taekwondo", "taekwondo")
    _carry(store, "spin", "wikidata", "action film", "action film")
    _carry(store, "other", "tmdb", "action", "action")
    _carry(store, "kick", "tmdb", "tae kwon do", "tae kwon do")
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_score, jev_model, judged_at) "
            "VALUES ('action', 'action film', 1.0, ?, ?)",
            (FILM_SUFFIX_MODEL, JUDGED),
        )
        conn.commit()
    _send(
        "POST",
        f"{base_url}/api/merges",
        {"keyword_a": "tae kwon do", "keyword_b": "taekwondo", "decision": "accepted"},
    )

    status, body = _send("GET", f"{base_url}/api/doctor?item_id=spin")

    assert status == 200
    by_source = {s["source"]: s["rows"] for s in body["sources"]}  # type: ignore[attr-defined]
    [tmdb], [wikidata] = by_source["tmdb"], by_source["wikidata"]
    assert (wikidata["surface"], wikidata["keyword"], wikidata["into"]) == (
        "action film",
        "action film",
        "action",
    )
    assert wikidata["via"] == {"score": 1.0, "model": FILM_SUFFIX_MODEL, "decision": None}
    assert (tmdb["keyword"], tmdb["into"]) == ("taekwondo", None)
    # The merge proposed above waits in the file; the doctor shows it before the sweep folds it.
    assert tmdb["pending"] == [
        {"other": "tae kwon do", "label": "tae kwon do", "decision": "accepted"}
    ]
    assert _send("GET", f"{base_url}/api/doctor?item_id=nope")[0] == 404


def test_the_doctor_names_no_pair_for_a_fold_through_the_group_and_hides_dead_decisions(
    base_url: str, store: Path
) -> None:
    _carry(store, "spin", "tmdb", "karate", "karate")
    _carry(store, "other", "tmdb", "karate film", "karate film")
    _carry(store, "other", "tmdb", "karate movie", "karate movie")
    _carry(store, "more", "tmdb", "karate movie", "karate movie")
    with open_store(store) as conn:
        # karate ~ karate film and karate film ~ karate movie merge, so karate folds into
        # karate movie (two titles) through karate film; the direct pair is one Jev refused.
        conn.executemany(
            "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_score, jev_model, judged_at) "
            "VALUES (?, ?, 0.95, 'jev-1', ?)",
            [("karate", "karate film", JUDGED), ("karate film", "karate movie", JUDGED)],
        )
        conn.execute(
            "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_error, judged_at) "
            "VALUES ('karate', 'karate movie', 'Jev returned 400', ?)",
            (JUDGED,),
        )
        conn.commit()
    # A decision on a keyword no title carries never reaches the table.
    MergeDecisions(store.parent / "merge_decisions.json").record(("gone", "karate"), "accepted", {})

    _, body = _send("GET", f"{base_url}/api/doctor?item_id=spin")

    [row] = body["sources"][0]["rows"]  # type: ignore[index]
    assert (row["into"], row["via"], row["pending"]) == ("karate movie", None, [])
    assert "gone" not in json.dumps(_send("GET", f"{base_url}/api/merges")[1])


def test_the_doctor_title_search_lists_only_titles_a_source_sent_keywords_for(
    base_url: str, store: Path
) -> None:
    _carry(store, "kick", "tmdb", "karate", "karate")
    _carry(store, "spin kick", "tmdb", "taekwondo", "taekwondo")
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO items (item_id, type, title) VALUES ('bare', 'movie', 'Kick Off')"
        )
        conn.commit()

    status, body = _send("GET", f"{base_url}/api/doctor/titles?q=kick")

    assert status == 200
    # "Kick Off" has no keyword a source sent, so there is nothing to doctor.
    assert [t["title"] for t in body["titles"]] == ["Kick", "Spin Kick"]  # type: ignore[attr-defined]
    assert _send("GET", f"{base_url}/api/doctor/titles?q=k")[0] == 400


def test_the_vocabulary_search_finds_a_keyword_by_a_raw_spelling_most_carried_first(
    base_url: str, store: Path
) -> None:
    _carry(store, "spin", "tmdb", "Taekwondo", "taekwondo")
    _carry(store, "kick", "tmdb", "Taekwondo", "taekwondo")
    _carry(store, "kick", "anilist", "Tae Kwon Do", "tae kwon do")
    _carry(store, "kick", "anilist", "Martial Arts", "martial art")

    status, body = _send("GET", f"{base_url}/api/keywords?q=arts")
    assert status == 200
    assert [(k["value"], k["titles"]) for k in body["keywords"]] == [("martial art", 1)]  # type: ignore[attr-defined]

    _, body = _send("GET", f"{base_url}/api/keywords?q=tae")
    assert [k["value"] for k in body["keywords"]] == ["taekwondo", "tae kwon do"]  # type: ignore[attr-defined]
    assert _send("GET", f"{base_url}/api/keywords?q=t")[0] == 400


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
    decisions.record(("a", "b"), "accepted", {})
    decisions.record(("c", "d"), "accepted", {})
    with pytest.raises(ValueError, match="too many merge decisions"):
        decisions.record(("e", "f"), "accepted", {})
    assert len(decisions.latest()) == 2

    small = MergeDecisions(tmp_path / "small.json")
    small.MAX_TOTAL_BYTES = 100
    with pytest.raises(ValueError, match="exceed"):
        small.record(("a", "b"), "accepted", {})
    assert small.latest() == {}


def test_a_write_replaces_every_spelling_of_its_pair_and_refuses_one_naming_another(
    tmp_path: Path,
) -> None:
    decisions = MergeDecisions(tmp_path / "merge_decisions.json")
    decisions.record(("heist", "vault"), "rejected", {"heist": ["Heists"]})
    decisions.record(("heist", "vault"), "accepted", {"heist": ["heist"], "vault": ["Vaults"]})

    entries = json.loads(decisions.path.read_text())
    assert [(e["surface_a"], e["surface_b"], e["decision"]) for e in entries] == [
        ("heist", "Vaults", "accepted")
    ]
    assert {k: e["decision"] for k, e in decisions.latest().items()} == {
        ("heist", "vault"): "accepted"
    }
    with pytest.raises(ValueError, match="normalizes back"):
        decisions.record(("heist", "vault"), "accepted", {"vault": ["Vault Doors"]})


def test_an_accept_spells_a_keyword_by_a_longer_surface_when_its_shortest_names_another(
    base_url: str, store: Path
) -> None:
    # `bk` is the shortest surface stored under `bank` but normalizes to `bk`; only
    # `Banks` resolves back, so the entry must carry `Banks`.
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO keyword_forms (surface, keyword) VALUES ('bk', 'bank'), ('Banks', 'bank')"
        )
        conn.commit()

    status, _ = _send(
        "POST",
        f"{base_url}/api/merges",
        {"keyword_a": "bank", "keyword_b": "vault", "decision": "accepted"},
    )

    assert status == 200
    entries = json.loads((store.parent / "merge_decisions.json").read_text())
    assert [(e["surface_a"], e["surface_b"]) for e in entries] == [("Banks", "vault")]


def test_merged_pairs_chain_into_a_group_named_by_its_most_carried_keyword_not_x_film(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plexdb.db"
    init(path)
    at = "2026-10-09T00:00:00Z"
    with open_store(path) as conn:
        conn.executemany(
            "INSERT INTO items (item_id, type, title) VALUES (?, 'movie', ?)",
            [("t1", "a"), ("t2", "b"), ("t3", "c")],
        )
        conn.executemany(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
            "VALUES (?, 'keywords', 'tmdb', 'keyword', ?, ?)",
            [
                ("t1", "kid", at),
                ("t2", "kid", at),
                ("t3", "child", at),
                ("t3", "youngster", at),
                ("t1", "slasher film", at),
                ("t2", "slasher film", at),
                ("t3", "slasher", at),
            ],
        )
        conn.executemany(
            "INSERT INTO keyword_pairs "
            "(keyword_a, keyword_b, jev_score, jev_model, judged_at, decision, decided_at) "
            "VALUES (?, ?, ?, 'jev-1', ?, ?, ?)",
            [
                ("child", "kid", 0.95, at, None, None),
                ("child", "youngster", 0.6, at, "accepted", at),
                ("heist", "robbery", 0.97, at, "rejected", at),
                ("dog", "puppy", 0.8, at, None, None),
            ],
        )
        conn.execute(
            "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_score, jev_model, judged_at) "
            "VALUES ('slasher', 'slasher film', 1.0, ?, ?)",
            (FILM_SUFFIX_MODEL, at),
        )
        conn.commit()
        folded = merge_map(conn)
    assert folded == {"child": "kid", "youngster": "kid", "slasher film": "slasher"}
