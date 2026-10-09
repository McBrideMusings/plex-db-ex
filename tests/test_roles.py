"""Asking Jev what roles each stored keyword plays.

Jev is faked: a fake judge answers fixed scores per role and records every tag
it is asked about. `LiveJev.judge_roles` is driven through `httpx.MockTransport`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from plexdb import roles
from plexdb.cli import main
from plexdb.commands import judge_keyword_roles as roles_cmd
from plexdb.errors import JevError, JevRejected
from plexdb.jev_client import LiveJev, RoleVerdict
from plexdb.keywords import ROLES, upsert_keyword_form
from plexdb.roles import judge_keyword_roles
from plexdb.store import init as init_store
from plexdb.store import open_store

MODEL = "jev-test-1.0"
SCORES = {"tone": 0.1, "era": 0.02, "region": 0.93, "theme": 0.4, "character_trait": 0.0}


class FakeRoleJudge:
    """Answers `SCORES` for every tag; raises `JevRejected` for a tag in `reject`,
    and `JevError` on the `fail_on_call`th ask."""

    def __init__(
        self, reject: frozenset[str] = frozenset(), fail_on_call: int | None = None
    ) -> None:
        self.asked: list[str] = []
        self._reject = reject
        self._fail_on_call = fail_on_call
        self._lock = threading.Lock()

    def judge_roles(self, tag: str) -> RoleVerdict:
        with self._lock:
            self.asked.append(tag)
            if len(self.asked) == self._fail_on_call:
                raise JevError("scripted failure")
        if tag in self._reject:
            raise JevRejected("Jev returned 400 for the test")
        return RoleVerdict(scores=dict(SCORES), model=MODEL)


def _store(tmp_path: Path, surfaces: list[str]) -> Path:
    path = tmp_path / "plexdb.db"
    init_store(path)
    with open_store(path) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'T')")
        for surface in surfaces:
            conn.execute(
                "INSERT OR IGNORE INTO enrichment "
                "(item_id, namespace, source, key, value, fetched_at) "
                "VALUES ('imdb:tt1', 'keywords', 'tmdb', 'keyword', ?, '2026-01-01T00:00:00Z')",
                (upsert_keyword_form(conn, surface),),
            )
        conn.commit()
    return path


def _rows(store: Path) -> list[sqlite3.Row]:
    with open_store(store) as conn:
        return list(
            conn.execute("SELECT * FROM keyword_roles WHERE source = 'jev' ORDER BY keyword, role")
        )


def _run(store: Path, judge: FakeRoleJudge, **kwargs: Any) -> roles.RoleStats:
    with open_store(store) as conn:
        return judge_keyword_roles(conn, judge, **kwargs)


def test_each_unjudged_keyword_gets_five_rows_from_one_request_about_its_shortest_surface(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, ["Los Angeles", "Heists", "heist", "grief"])
    judge = FakeRoleJudge()

    stats = _run(store, judge)

    assert sorted(judge.asked) == ["Los Angeles", "grief", "heist"]
    rows = _rows(store)
    assert len(rows) == 15
    assert stats.keywords_judged == 3 and stats.rows_written == 15
    la = {row["role"]: row for row in rows if row["keyword"] == "los angeles"}
    assert set(la) == set(ROLES)
    assert {role: row["score"] for role, row in la.items()} == SCORES
    assert {row["model"] for row in rows} == {MODEL}
    assert all(row["error"] is None and row["stated_at"] for row in rows)


def test_a_keyword_with_no_surface_form_is_asked_about_as_stored(tmp_path: Path) -> None:
    store = _store(tmp_path, ["heist"])
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
            "VALUES ('imdb:tt1', 'keywords', 'tmdb', 'keyword', 'bank robberi', "
            "'2026-01-01T00:00:00Z')"
        )
        conn.commit()
    judge = FakeRoleJudge()

    _run(store, judge)

    assert sorted(judge.asked) == ["bank robberi", "heist"]


def test_a_second_run_with_no_new_keyword_sends_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path, ["heist", "grief"])
    _run(store, FakeRoleJudge())
    first = [tuple(row) for row in _rows(store)]

    again = FakeRoleJudge()
    stats = _run(store, again)

    assert again.asked == []
    assert stats.keywords_already_judged == 2 and stats.rows_written == 0
    assert [tuple(row) for row in _rows(store)] == first


def test_other_sources_rows_do_not_count_as_judged(tmp_path: Path) -> None:
    store = _store(tmp_path, ["Tokyo"])
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO keyword_roles (keyword, role, source, stated_at) "
            "VALUES ('tokyo', 'region', 'wikidata', '2026-01-01T00:00:00Z')"
        )
        conn.commit()
    judge = FakeRoleJudge()

    _run(store, judge)

    assert judge.asked == ["Tokyo"]
    assert len(_rows(store)) == 5


def test_a_refused_keyword_is_stored_and_never_asked_again(tmp_path: Path) -> None:
    store = _store(tmp_path, ["heist", "grief", "zebra"])
    judge = FakeRoleJudge(reject=frozenset({"grief"}))

    stats = _run(store, judge)

    assert stats.keywords_unjudgeable == 1 and stats.keywords_judged == 2
    refused = [row for row in _rows(store) if row["keyword"] == "grief"]
    assert len(refused) == 5
    assert all(r["score"] is None and r["model"] is None and "400" in r["error"] for r in refused)
    assert all(r["error"] is None for r in _rows(store) if r["keyword"] != "grief")

    again = FakeRoleJudge(reject=frozenset({"grief"}))
    _run(store, again)
    assert again.asked == []


def test_a_run_killed_midway_keeps_every_finished_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(roles, "KEYWORD_BATCH", 2)
    monkeypatch.setattr(roles, "JUDGE_WORKERS", 1)
    store = _store(tmp_path, ["alpha", "bravo", "charlie", "delta", "echo"])

    with pytest.raises(JevError):
        _run(store, FakeRoleJudge(fail_on_call=4))

    assert sorted({row["keyword"] for row in _rows(store)}) == ["alpha", "bravo"]
    resumed = FakeRoleJudge()
    _run(store, resumed)
    assert resumed.asked == ["charlie", "delta", "echo"]
    assert len(_rows(store)) == 25


def test_limit_caps_the_keywords_asked_about(tmp_path: Path) -> None:
    store = _store(tmp_path, ["alpha", "bravo", "charlie"])
    judge = FakeRoleJudge()

    _run(store, judge, limit=2)

    assert judge.asked == ["alpha", "bravo"]
    assert len(_rows(store)) == 10


def test_the_command_runs_through_the_cli_and_needs_only_the_jev_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path, ["heist", "grief"])
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("LLAMA_BROKER_BASE_URL", "")
    monkeypatch.setenv("TYPESAFE_API_KEY", "")

    assert main(["judge-keyword-roles"]) == 1
    assert "TYPESAFE_API_KEY" in capsys.readouterr().err

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-jev-key")
    monkeypatch.setattr(roles_cmd, "LiveJev", lambda key: FakeRoleJudge())
    assert main(["judge-keyword-roles", "--limit", "1"]) == 0
    assert "1 judged now, 0 unjudgeable, 5 row(s) written" in capsys.readouterr().out
    assert len(_rows(store)) == 5


def _jev_http(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _role_answer(scores: dict[str, float]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "jev-1.13.0",
            "answers": {role: {"type": "noul", "noul": s} for role, s in scores.items()},
        },
    )


def test_jev_asks_one_noul_question_per_role_about_the_tag() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _role_answer(SCORES)

    verdict = LiveJev("the-test-key", _jev_http(handler), lambda _s: None).judge_roles("Tokyo")

    assert verdict == RoleVerdict(scores=SCORES, model="jev-1.13.0")
    assert len(seen) == 1
    body = json.loads(seen[0].content)
    assert body["state"]["tag"] == "Tokyo"
    assert body["model"] == "jev-latest"
    assert {key: q["type"] for key, q in body["questions"].items()} == {r: "noul" for r in ROLES}
    assert seen[0].headers["authorization"] == "Bearer the-test-key"


def test_jev_role_answers_missing_a_role_or_out_of_range_are_refused() -> None:
    missing = {role: 0.5 for role in ROLES if role != "era"}
    out_of_range = {**SCORES, "tone": 1.2}
    for scores in (missing, out_of_range):
        client = LiveJev("k", _jev_http(lambda r, s=scores: _role_answer(s)), lambda _s: None)
        with pytest.raises(JevError) as err:
            client.judge_roles("a")
        assert not isinstance(err.value, JevRejected)


def test_jev_marks_a_400_role_request_as_rejected() -> None:
    client = LiveJev("k", _jev_http(lambda r: httpx.Response(422)), lambda _s: None)
    with pytest.raises(JevRejected):
        client.judge_roles("a")
