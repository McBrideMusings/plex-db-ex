"""Finding synonym keywords and folding a person's decisions into the table.

Both services are faked: a fixed vector per keyword stands in for the embedding
server, and a fake judge records every pair it is asked about. The fixture
store holds six keywords with these cosines (>= 0.75 is proposed):

    heist, bank robbery, robbery   0.90 / 0.95 / 0.85 between the three
    prison, jail                   0.99
    zebra                          under 0.5 to everything
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

from plexdb import synonyms
from plexdb.cli import main
from plexdb.commands import judge_keyword_pairs as judge_cmd
from plexdb.config import Config
from plexdb.embed_client import LlamaSwapEmbedder
from plexdb.errors import EmbeddingError, JevError, JevRejected, MergeDecisionsError
from plexdb.jev_client import LiveJev, Verdict
from plexdb.keywords import normalize_keyword, upsert_keyword_form
from plexdb.store import init as init_store
from plexdb.store import open_store
from plexdb.synonyms import find_synonym_pairs, fold_merge_decisions, merge_decisions_path

VECTORS = {
    "heist": [1.0, 0.0, 0.0],
    "bank robbery": [0.95, 0.31, 0.0],
    "robbery": [0.9, 0.0, 0.44],
    "prison": [0.0, 1.0, 0.0],
    "jail": [0.1, 0.99, 0.0],
    "zebra": [0.0, 0.0, 1.0],
}
HEIST, BANK, ROBBERY, PRISON, JAIL, ZEBRA = (normalize_keyword(s) for s in VECTORS)
MODEL = "jev-test-1.0"


def _p(a: str, b: str) -> tuple[str, str]:
    """A pair as the table stores it, `keyword_a < keyword_b`."""
    return (a, b) if a < b else (b, a)


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [VECTORS[text] for text in texts]


class FakeJudge:
    """Answers 0.8 for every pair; `fail_on_call` raises on that 1-indexed ask, and
    `reject_on_call` raises `JevRejected` on that one."""

    def __init__(self, fail_on_call: int | None = None, reject_on_call: int | None = None) -> None:
        self.asked: list[tuple[str, str]] = []
        self._fail_on_call = fail_on_call
        self._reject_on_call = reject_on_call
        self._lock = threading.Lock()

    def judge(self, tag_a: str, tag_b: str) -> Verdict:
        with self._lock:
            self.asked.append((tag_a, tag_b))
            if len(self.asked) == self._fail_on_call:
                raise JevError("scripted failure")
            if len(self.asked) == self._reject_on_call:
                raise JevRejected("Jev returned 400 for the test")
        return Verdict(score=0.8, model=MODEL)


def _store(tmp_path: Path, surfaces: list[str] | None = None) -> Path:
    path = tmp_path / "plexdb.db"
    init_store(path)
    with open_store(path) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'T')")
        for surface in VECTORS if surfaces is None else surfaces:
            value = upsert_keyword_form(conn, surface)
            conn.execute(
                "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
                "VALUES ('imdb:tt1', 'keywords', 'tmdb', 'keyword', ?, '2026-01-01T00:00:00Z')",
                (value,),
            )
        conn.commit()
    return path


def _pair_rows(store: Path) -> list[sqlite3.Row]:
    with open_store(store) as conn:
        return list(conn.execute("SELECT * FROM keyword_pairs ORDER BY keyword_a, keyword_b"))


def _pair_keys(store: Path) -> list[tuple[str, str]]:
    return [(row["keyword_a"], row["keyword_b"]) for row in _pair_rows(store)]


def _run(store: Path, judge: FakeJudge, embedder: FakeEmbedder, **kwargs: Any) -> Any:
    with open_store(store) as conn:
        return find_synonym_pairs(conn, embedder, judge, **kwargs)


def test_six_keywords_yield_four_pairs_stored_as_jev_answered_and_a_rerun_adds_none(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    judge = FakeJudge()

    stats = _run(store, judge, FakeEmbedder())

    expected = sorted([_p(HEIST, BANK), _p(HEIST, ROBBERY), _p(BANK, ROBBERY), _p(JAIL, PRISON)])
    assert _pair_keys(store) == expected
    assert (stats.keywords_examined, stats.pairs_proposed, stats.pairs_judged) == (6, 4, 4)
    rows = _pair_rows(store)
    assert {(r["jev_score"], r["jev_model"]) for r in rows} == {(0.8, MODEL)}
    assert all(r["keyword_a"] < r["keyword_b"] and r["judged_at"] for r in rows)
    assert all(r["decision"] is None and r["decided_at"] is None for r in rows)
    assert len(judge.asked) == 4
    assert {frozenset(pair) for pair in judge.asked} == {
        frozenset({"heist", "bank robbery"}),
        frozenset({"heist", "robbery"}),
        frozenset({"bank robbery", "robbery"}),
        frozenset({"jail", "prison"}),
    }

    again = FakeJudge()
    stats = _run(store, again, FakeEmbedder())

    assert again.asked == []
    assert (stats.keywords_examined, stats.pairs_proposed, stats.pairs_judged) == (6, 4, 0)
    assert len(_pair_rows(store)) == 4


def test_jev_is_asked_about_the_readable_surface_not_the_stem(tmp_path: Path) -> None:
    store = _store(tmp_path, ["robbery", "bank robbery"])
    judge = FakeJudge()
    embedder = FakeEmbedder()

    _run(store, judge, embedder)

    assert normalize_keyword("robbery") == "robberi"
    assert [sorted(call) for call in embedder.calls] == [["bank robbery", "robbery"]]
    assert [frozenset(pair) for pair in judge.asked] == [frozenset({"bank robbery", "robbery"})]


def test_a_pair_that_has_a_row_keeps_its_decision_and_is_not_asked_again(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with open_store(store) as conn, conn:
        conn.execute(
            "INSERT INTO keyword_pairs "
            "(keyword_a, keyword_b, jev_score, jev_model, judged_at, decision, decided_at) "
            "VALUES (?, ?, 0.1, 'jev-old', '2026-01-01T00:00:00+00:00', "
            "'rejected', '2026-01-02T00:00:00+00:00')",
            (HEIST, ROBBERY),
        )
    judge = FakeJudge()

    _run(store, judge, FakeEmbedder())

    assert len(_pair_rows(store)) == 4
    rows = {(r["keyword_a"], r["keyword_b"]): r for r in _pair_rows(store)}
    kept = rows[(HEIST, ROBBERY)]
    assert (kept["jev_score"], kept["jev_model"], kept["decision"]) == (0.1, "jev-old", "rejected")
    assert frozenset({"heist", "robbery"}) not in {frozenset(p) for p in judge.asked}
    assert len(judge.asked) == 3


def test_a_failure_partway_keeps_the_batches_already_written_and_a_rerun_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(synonyms, "KEYWORD_BATCH", 1)
    store = _store(tmp_path)

    with pytest.raises(JevError):
        _run(store, FakeJudge(fail_on_call=4), FakeEmbedder())

    # Batches "bank robberi" (2 pairs) and "heist" (1 new pair) committed; "jail" failed whole.
    assert _pair_keys(store) == sorted([_p(HEIST, BANK), _p(BANK, ROBBERY), _p(HEIST, ROBBERY)])

    _run(store, FakeJudge(), FakeEmbedder())

    assert len(_pair_rows(store)) == 4


def test_a_pair_jev_refuses_is_stored_with_its_error_and_the_rest_of_the_batch_lands(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    stats = _run(store, FakeJudge(reject_on_call=2), FakeEmbedder())

    rows = _pair_rows(store)
    assert len(rows) == 4
    refused = [row for row in rows if row["jev_error"] is not None]
    assert len(refused) == 1
    assert (refused[0]["jev_score"], refused[0]["jev_model"]) == (None, None)
    assert "400" in refused[0]["jev_error"]
    assert (stats.pairs_judged, stats.pairs_unjudgeable) == (3, 1)

    again = FakeJudge()
    _run(store, again, FakeEmbedder())
    assert again.asked == []


def test_limit_caps_the_pairs_asked_and_a_rerun_finishes_the_rest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    judge = FakeJudge()

    stats = _run(store, judge, FakeEmbedder(), limit=3)

    assert (stats.pairs_judged, len(judge.asked)) == (3, 3)
    assert len(_pair_rows(store)) == 3

    _run(store, FakeJudge(), FakeEmbedder(), limit=3)

    assert len(_pair_rows(store)) == 4


def test_a_keyword_whose_only_row_is_another_keywords_pair_still_proposes_its_own(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with open_store(store) as conn, conn:
        conn.execute(
            "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_score, jev_model, judged_at) "
            "VALUES (?, ?, 0.9, ?, '2026-01-01T00:00:00+00:00')",
            (_p(HEIST, BANK)[0], _p(HEIST, BANK)[1], MODEL),
        )
    judge = FakeJudge()

    _run(store, judge, FakeEmbedder())

    assert len(_pair_rows(store)) == 4
    assert len(judge.asked) == 3


def test_a_fully_judged_store_asks_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path, ["prison", "jail"])
    with open_store(store) as conn, conn:
        conn.execute(
            "INSERT INTO keyword_pairs "
            "(keyword_a, keyword_b, jev_score, jev_model, judged_at, decision, decided_at) "
            "VALUES (?, ?, 0.9, ?, '2026-01-01T00:00:00+00:00', "
            "NULL, NULL)",
            (JAIL, PRISON, MODEL),
        )
    judge = FakeJudge()

    _run(store, judge, FakeEmbedder())

    assert judge.asked == []


def test_a_cached_vocabulary_is_never_embedded_again_only_new_keywords_are(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    cache = tmp_path / "keyword-embeddings.npz"
    first = FakeEmbedder()
    _run(store, FakeJudge(), first, cache_path=cache)
    assert sorted(t for call in first.calls for t in call) == sorted(VECTORS)

    second = FakeEmbedder()
    _run(store, FakeJudge(), second, cache_path=cache)
    assert second.calls == []

    VECTORS["mugshot"] = [0.0, 0.9, 0.3]
    try:
        with open_store(store) as conn, conn:
            value = upsert_keyword_form(conn, "mugshot")
            conn.execute(
                "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
                "VALUES ('imdb:tt1', 'keywords', 'tmdb', 'keyword', ?, '2026-01-01T00:00:00Z')",
                (value,),
            )
        third = FakeEmbedder()
        _run(store, FakeJudge(), third, cache_path=cache)
    finally:
        del VECTORS["mugshot"]
    assert third.calls == [["mugshot"]]


def test_a_cache_made_by_another_model_is_discarded_and_rebuilt(tmp_path: Path) -> None:
    store = _store(tmp_path, ["prison", "jail", "zebra"])
    cache = tmp_path / "keyword-embeddings.npz"
    _run(store, FakeJudge(), FakeEmbedder(), cache_path=cache)
    with np.load(cache) as data:
        other = {name: data[name] for name in data.files}
    other["model"] = np.array("some-other-model")
    np.savez(cache, **other)
    embedder = FakeEmbedder()

    _run(store, FakeJudge(), embedder, cache_path=cache)

    assert sorted(t for call in embedder.calls for t in call) == ["jail", "prison", "zebra"]


def _decisions_file(tmp_path: Path, entries: object) -> Path:
    path = tmp_path / "merge_decisions.json"
    path.write_text(json.dumps(entries))
    return path


def _entry(a: str, b: str, decision: str, at: str = "2026-10-01T12:00:00+00:00") -> dict[str, str]:
    return {"keyword_a": a, "keyword_b": b, "decision": decision, "decided_at": at}


def _decisions(store: Path) -> dict[tuple[str, str], tuple[str | None, str | None]]:
    return {
        (r["keyword_a"], r["keyword_b"]): (r["decision"], r["decided_at"])
        for r in _pair_rows(store)
    }


def test_folding_sets_clears_and_counts_decisions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _run(store, FakeJudge(), FakeEmbedder())
    at = "2026-10-01T12:00:00+00:00"
    path = _decisions_file(
        tmp_path,
        [
            _entry(BANK, HEIST, "accepted"),
            _entry(JAIL, PRISON, "rejected"),
            _entry(JAIL, PRISON, "cleared"),
            _entry(HEIST, ROBBERY, "rejected"),
            _entry("aaa", "bbb", "accepted"),
        ],
    )

    with open_store(store) as conn:
        stats = fold_merge_decisions(conn, path)

    assert (stats.entries, stats.matched, stats.unmatched) == (5, 4, 1)
    decisions = _decisions(store)
    assert decisions[(BANK, HEIST)] == ("accepted", at)
    assert decisions[(HEIST, ROBBERY)] == ("rejected", at)
    assert decisions[(JAIL, PRISON)] == (None, None)
    assert decisions[(BANK, ROBBERY)] == (None, None)


def test_a_missing_decisions_file_changes_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with open_store(store) as conn:
        stats = fold_merge_decisions(conn, tmp_path / "merge_decisions.json")
    assert (stats.file_found, stats.entries) == (False, 0)


@pytest.mark.parametrize(
    "entries",
    [
        {"not": "a list"},
        [_entry(BANK, HEIST, "maybe")],
        [_entry(HEIST, BANK, "accepted")],
        [_entry(BANK, HEIST, "accepted", at="")],
        [_entry(BANK, HEIST, "accepted", at="yesterday")],
        [{"keyword_a": BANK, "keyword_b": HEIST, "decision": "accepted"}],
    ],
)
def test_a_malformed_decisions_file_is_refused_before_any_row_changes(
    tmp_path: Path, entries: object
) -> None:
    store = _store(tmp_path)
    _run(store, FakeJudge(), FakeEmbedder())
    if isinstance(entries, list):
        entries = [_entry(JAIL, PRISON, "accepted"), *entries]
    path = _decisions_file(tmp_path, entries)

    with open_store(store) as conn, pytest.raises(MergeDecisionsError):
        fold_merge_decisions(conn, path)

    assert all(decision == (None, None) for decision in _decisions(store).values())


def test_the_decisions_file_sits_beside_the_saved_queries_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "data" / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("PLEXDB_SNAPSHOT_PATH", "")
    monkeypatch.setenv("PLEXDB_EXPLORE_SAVED_PATH", "")
    assert merge_decisions_path(Config.from_env()) == tmp_path / "data" / "merge_decisions.json"

    monkeypatch.setenv("PLEXDB_SNAPSHOT_PATH", str(tmp_path / "snap" / "plexdb.db"))
    assert merge_decisions_path(Config.from_env()) == tmp_path / "snap" / "merge_decisions.json"

    monkeypatch.setenv("PLEXDB_EXPLORE_SAVED_PATH", str(tmp_path / "explore" / "q.json"))
    assert merge_decisions_path(Config.from_env()) == tmp_path / "explore" / "merge_decisions.json"


def test_the_commands_run_through_the_cli_and_skip_when_a_service_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path)
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("PLEXDB_SNAPSHOT_PATH", "")
    monkeypatch.setenv("PLEXDB_EXPLORE_SAVED_PATH", "")
    _decisions_file(store.parent, [])
    monkeypatch.setenv("LLAMA_BROKER_BASE_URL", "")
    monkeypatch.setenv("TYPESAFE_API_KEY", "")

    assert main(["judge-keyword-pairs"]) == 1
    assert "LLAMA_BROKER_BASE_URL" in capsys.readouterr().err

    monkeypatch.setenv("LLAMA_BROKER_BASE_URL", "http://embed.test")
    assert main(["judge-keyword-pairs"]) == 1
    assert "TYPESAFE_API_KEY" in capsys.readouterr().err

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-jev-key")
    monkeypatch.setattr(judge_cmd, "LlamaSwapEmbedder", lambda url: FakeEmbedder())
    monkeypatch.setattr(judge_cmd, "LiveJev", lambda key: FakeJudge())
    assert main(["judge-keyword-pairs"]) == 0
    assert "4 judged now" in capsys.readouterr().out
    assert len(_pair_rows(store)) == 4

    assert main(["fold-merge-decisions"]) == 0
    assert "0 entr(ies)" in capsys.readouterr().out


def _jev_http(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _jev_ok(noul: float = 0.8) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "jev-1.13.0",
            "answers": {"s": {"type": "score", "score": 1.5}, "n": {"type": "noul", "noul": noul}},
        },
    )


def test_jev_retries_a_429_with_doubling_backoff_and_returns_the_noul_answer() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(429) if len(seen) <= 2 else _jev_ok()

    sleeps: list[float] = []
    verdict = LiveJev("the-test-key", _jev_http(handler), sleeps.append).judge("heist", "robbery")

    assert verdict == Verdict(score=0.8, model="jev-1.13.0")
    assert sleeps == [0.5, 1.0]
    body = json.loads(seen[0].content)
    assert body["state"] == {"tag_a": "heist", "tag_b": "robbery"}
    assert body["model"] == "jev-latest"
    assert {name: q["type"] for name, q in body["questions"].items()} == {
        "s": "score",
        "n": "noul",
    }
    assert seen[0].headers["authorization"] == "Bearer the-test-key"


def test_jev_gives_up_after_eight_429s_without_printing_the_key() -> None:
    sleeps: list[float] = []
    client = LiveJev("the-test-key", _jev_http(lambda r: httpx.Response(429)), sleeps.append)

    with pytest.raises(JevError) as err:
        client.judge("a", "b")

    assert len(sleeps) == 8
    assert "the-test-key" not in str(err.value)


def test_jev_marks_a_400_or_422_as_rejected_and_any_other_status_as_a_plain_failure() -> None:
    for status in (400, 422):
        client = LiveJev("k", _jev_http(lambda r, s=status: httpx.Response(s)), lambda _s: None)
        with pytest.raises(JevRejected):
            client.judge("a", "b")
    for status in (401, 403, 500, 503):
        client = LiveJev("k", _jev_http(lambda r, s=status: httpx.Response(s)), lambda _s: None)
        with pytest.raises(JevError) as err:
            client.judge("a", "b")
        assert not isinstance(err.value, JevRejected)


def test_jev_refuses_a_score_outside_zero_to_one() -> None:
    client = LiveJev("k", _jev_http(lambda r: _jev_ok(noul=1.4)), lambda _s: None)
    with pytest.raises(JevError):
        client.judge("a", "b")


def test_the_embedder_returns_vectors_in_input_order_and_names_a_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "nomic-embed-text"
        if body["input"] == ["boom"]:
            return httpx.Response(500, text="upstream command exited prematurely")
        data = [{"index": 1, "embedding": [0.0, 2.0]}, {"index": 0, "embedding": [1.0, 0.0]}]
        return httpx.Response(200, json={"data": data})

    embedder = LlamaSwapEmbedder("http://embed.test/", _jev_http(handler))

    assert embedder.embed(["a", "b"]) == [[1.0, 0.0], [0.0, 2.0]]
    with pytest.raises(EmbeddingError, match="500.*exited prematurely"):
        embedder.embed(["boom"])
