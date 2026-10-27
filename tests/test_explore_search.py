"""The Search tab's server (`plexdb.explore_search`) and its page."""

from __future__ import annotations

import ast
import json
import math
import re
import sqlite3
import stat
import statistics
import struct
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fixture_store import init
from playwright.sync_api import Browser, sync_playwright
from tokenizers import Tokenizer, models, pre_tokenizers

from plexdb.config import Config
from plexdb.errors import PlexError
from plexdb.explore import make_server
from plexdb.explore_search import (
    TOKEN_LIMIT,
    MediaCache,
    NoFrame,
    PhraseTokenizer,
    SearchTab,
)
from plexdb.frames import migrate, open_frames
from plexdb.plex_client import Section
from plexdb.store import open_store

MODEL = "test/clip"
NOW = "2026-10-16T00:00:00+00:00"


def _tokenizer() -> Tokenizer:
    """Words are tokens; `fight` after `fire` is a separate piece, as in CLIP's vocabulary."""
    vocab = {
        w: i for i, w in enumerate(["[UNK]", "fire", "##fight", "sunny", "in", "los", "angeles"])
    }
    tok = Tokenizer(models.WordPiece(vocab, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    return tok


@pytest.fixture
def phrase_tokenizer(tmp_path: Path) -> PhraseTokenizer:
    path = tmp_path / "tokenizer.json"
    _tokenizer().save(str(path))
    return PhraseTokenizer(MODEL, tmp_path / "tok", path)


class FakeMedia:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def frames(self, path: Path, times: list[int], *, height: int = 224) -> list[bytes | None]:
        self.calls.append(("frames", int(path.stem), tuple(times), height))
        return [b"\xff\xd8jpeg" for _ in times]

    def clip(self, path: Path, in_ms: int, out_ms: int, *, width: int = 640) -> bytes:
        self.calls.append(("clip", int(path.stem), in_ms, out_ms, width))
        return b"0123456789" * 10


READER = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}) + "\\n")
if os.environ.get("FAKE_STDERR"):
    sys.stderr.write(os.environ["FAKE_STDERR"])
    sys.exit(1)
draw = "--draw" in sys.argv
print(open(os.environ["FAKE_DRAWN" if draw else "FAKE_FOUND"]).read())
"""


def _shot(
    item_id: str, in_ms: int, out_ms: int, score: float, p: float, sims: list[float]
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "in_ms": in_ms,
        "out_ms": out_ms,
        "frames": [[in_ms, 0.3], [in_ms + 1000, 0.4], [in_ms + 2000, 0.2], [in_ms + 3000, 0.1]],
        "phrase_sim": score + 0.1,
        "negative_sims": sims,
        "score": score,
        "p": p,
    }


FOUND: dict[str, Any] = {
    "shots": [
        _shot("imdb:tt1", 0, 6300, 0.152, 0.61, [0.1, 0.33]),
        _shot("imdb:tt2", 1000, 1800, 0.147, 0.37, [0.30, 0.2]),
        _shot("imdb:tt1", 9000, 16100, 0.126, 0.02, [0.28, 0.1]),
    ],
    "indexed": 2,
    "pool": 3,
    "model": MODEL,
}


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    store = tmp_path / "plexdb.db"
    init(store)
    with open_store(store) as conn:
        for item_id, title, year in (
            ("imdb:tt1", "Heat", 1995),
            ("imdb:tt2", "La Haine", 1995),
            ("imdb:tt3", "Ronin", 1998),
        ):
            conn.execute(
                "INSERT INTO items (item_id, type, title, year) VALUES (?, 'movie', ?, ?)",
                (item_id, title, year),
            )
        conn.execute(
            "INSERT INTO collection (collection_id, source, name, observed_at) "
            "VALUES ('mdblist:1', 'mdblist', 'Heist films', ?)",
            (NOW,),
        )
        for rank, item_id in enumerate(("imdb:tt2", "imdb:tt1"), start=1):
            conn.execute(
                "INSERT INTO collection_membership (collection_id, item_id, rank, observed_at) "
                "VALUES ('mdblist:1', ?, ?, ?)",
                (item_id, rank, NOW),
            )
        conn.commit()
    frames_dir = tmp_path / "frames"
    migrate(frames_dir / "frames.db")
    with open_frames(frames_dir / "frames.db") as frames:
        for item_id, part, error in (("imdb:tt1", 11, None), ("imdb:tt2", 12, None)):
            frames.execute(
                "INSERT INTO frame_index_state (item_id, part_id, model, model_version, dims, "
                "quant, indexed_at, error) VALUES (?, ?, ?, 'v1', 768, 'int8', ?, ?)",
                (item_id, part, MODEL, NOW, error),
            )
        frames.commit()
    reader = tmp_path / "plexdb-read"
    reader.write_text(READER)
    reader.chmod(reader.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "reader.log"
    found, drawn = tmp_path / "found.json", tmp_path / "drawn.json"
    found.write_text(json.dumps(FOUND))
    drawn.write_text(json.dumps({**FOUND, "shots": [FOUND["shots"][2], FOUND["shots"][0]]}))
    for key, value in {
        "PLEXDB_PATH": str(store),
        "PLEXDB_FRAMES_DIR": str(frames_dir),
        "PLEXDB_PHRASE_CACHE": str(tmp_path / "phrases.db"),
        "PLEXDB_FRAME_ENCODER_URL": "http://encoder.invalid:1",
        "PLEXDB_FRAME_MODEL": MODEL,
        "PLEXDB_READ_BIN": str(reader),
        "PLEX_URL": "http://plex.invalid:32400",
        "PLEX_TOKEN": "secret",
        "FAKE_LOG": str(log),
        "FAKE_FOUND": str(found),
        "FAKE_DRAWN": str(drawn),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("FAKE_STDERR", raising=False)
    return {"store": store, "log": log, "tmp": tmp_path, "frames": frames_dir / "frames.db"}


@pytest.fixture
def served(world: dict[str, Any], phrase_tokenizer: PhraseTokenizer) -> Iterator[dict[str, Any]]:
    media = FakeMedia()
    tab = SearchTab(
        Config.from_env(),
        world["store"],
        world["tmp"] / "explore-data",
        tokenizer=phrase_tokenizer,
        media=lambda: media,  # type: ignore[arg-type,return-value]
        files=lambda _item_id, part: Path(f"/media/{part}.mkv"),
    )
    server = make_server(world["store"], "127.0.0.1", 0, search=tab)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield {**world, "url": f"http://127.0.0.1:{server.server_port}", "media": media, "tab": tab}
    finally:
        server.shutdown()
        server.server_close()


def _post(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url, json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def _calls(world: dict[str, Any]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in world["log"].read_text().splitlines()]


def test_a_split_word_is_two_boxes_and_the_phrase_is_cut_at_the_75th_token(
    phrase_tokenizer: PhraseTokenizer,
) -> None:
    said = phrase_tokenizer.analyse("sunny firefight in Los Angeles")
    assert said["count"] == 6
    tokens: list[dict[str, Any]] = said["tokens"]  # type: ignore[assignment]
    assert [t["text"] for t in tokens] == ["sunny", "fire", "fight", "in", "Los", "Angeles"]
    assert [(t["start"], t["end"]) for t in tokens[1:3]] == [(6, 10), (10, 15)]

    long = " ".join(["sunny"] * (TOKEN_LIMIT + 1))
    over = phrase_tokenizer.analyse(long)
    assert over["count"] == 76
    assert [t["over"] for t in over["tokens"]][-2:] == [False, True]  # type: ignore[attr-defined]
    assert phrase_tokenizer.searched(long) == " ".join(["sunny"] * TOKEN_LIMIT)


def test_a_missing_tokenizer_is_reported_not_guessed(tmp_path: Path) -> None:
    tokenizer = PhraseTokenizer(MODEL, tmp_path)
    problem = tokenizer.status()
    assert problem is not None and "PLEXDB_FRAME_TOKENIZER" in problem
    assert str(tmp_path / "tokenizer-test--clip.json") in problem
    assert not (tmp_path / "tokenizer-test--clip.json").exists()  # nothing was fetched or written


def test_search_runs_the_reader_with_each_negative_and_names_the_strongest(
    served: dict[str, Any],
) -> None:
    status, out = _post(
        served["url"] + "/api/search/run",
        {
            "phrase": "  sunny firefight ",
            "pool": ["imdb:tt1", "imdb:tt2", "imdb:tt3"],
            "negatives": ["gangland execution", "futuristic"],
            "weight": 0.5,
            "temperature": 0.01,
        },
    )
    assert status == 200
    argv = _calls(served)[0]["argv"]
    assert argv[argv.index("search") :] == [
        "search", "sunny firefight", "--pool-file", "-",
        "--not", "gangland execution", "--not", "futuristic",
        "--weight", "0.5", "--temperature", "0.01",
    ]  # fmt: skip
    assert _calls(served)[0]["stdin"] == "imdb:tt1\nimdb:tt2\nimdb:tt3"
    first, second = out["shots"][:2]
    assert (first["rank"], first["title"], first["in"], first["out"], first["seconds"]) == (
        1, "Heat", "0:00:00.0", "0:00:06.3", 6.3,
    )  # fmt: skip
    assert first["strongest_negative"] == {"phrase": "futuristic", "sim": 0.33}
    assert second["strongest_negative"] == {"phrase": "gangland execution", "sim": 0.30}
    assert first["thumbs"] == [0, 2000, 3000]
    assert (first["part_id"], second["part_id"]) == (11, 12)
    assert (out["indexed"], out["pool"], out["total"]) == (2, 3, 3)


def test_draw_marks_the_reader_picks_in_order_and_passes_the_seed(served: dict[str, Any]) -> None:
    status, out = _post(
        served["url"] + "/api/search/run",
        {
            "phrase": "fire",
            "pool": ["imdb:tt1"],
            "negatives": ["a", "b"],
            "draw": {"n": 2, "seed": 42},
        },
    )
    assert status == 200
    drawn_args = _calls(served)[1]["argv"]
    assert drawn_args[-4:] == ["--draw", "2", "--seed", "42"]
    assert {(s["rank"], s["drawn"]) for s in out["shots"]} == {(1, 2), (2, None), (3, 1)}
    assert out["draw"] == {"n": 2, "seed": 42, "picks": 2}


def test_a_refused_encoder_answers_503_naming_the_url_and_the_phrases(
    served: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_STDERR", "encoder unavailable at http://encoder.invalid:1: refused")
    status, out = _post(
        served["url"] + "/api/search/run",
        {"phrase": "fire", "pool": ["imdb:tt1"], "negatives": ["night"]},
    )
    assert status == 503
    assert out["encoder_down"]["url"] == "http://encoder.invalid:1"
    assert out["encoder_down"]["phrases"] == ["fire", "night"]


def test_the_pool_reports_each_titles_index_state_and_queue_writes_the_rest(
    served: dict[str, Any],
) -> None:
    status, pool = _post(
        served["url"] + "/api/search/pool", {"item_ids": ["imdb:tt1", "imdb:tt3", "nope"]}
    )
    assert status == 200
    assert {t["item_id"]: t["state"] for t in pool["titles"]} == {
        "imdb:tt1": "indexed",
        "imdb:tt3": "none",
    }
    assert pool["unknown"] == ["nope"] and pool["indexed"] == 1

    status, queued = _post(
        served["url"] + "/api/search/queue", {"item_ids": ["imdb:tt1", "imdb:tt3"]}
    )
    assert (status, queued["queued"]) == (200, 1)
    with open_frames(served["frames"]) as frames:
        row = frames.execute("SELECT item_id, queued_by FROM frame_queue").fetchone()
    assert tuple(row) == ("imdb:tt3", "explore")
    _, again = _post(served["url"] + "/api/search/pool", {"item_ids": ["imdb:tt3"]})
    assert again["titles"][0]["state"] == "queued"


def test_a_collection_expands_to_its_members_in_rank_order(served: dict[str, Any]) -> None:
    with urllib.request.urlopen(served["url"] + "/api/search/collection?id=mdblist:1") as response:
        assert json.loads(response.read())["item_ids"] == ["imdb:tt2", "imdb:tt1"]
    with urllib.request.urlopen(served["url"] + "/api/search/suggest?q=heist") as response:
        found = json.loads(response.read())
    assert found["collections"] == [
        {"collection_id": "mdblist:1", "name": "Heist films", "members": 2}
    ]


def test_a_clip_is_cut_once_served_in_ranges_and_found_in_the_cache(served: dict[str, Any]) -> None:
    url = served["url"] + "/api/search/clip?item_id=imdb:tt1&part=11&in_ms=1000&out_ms=7300"
    for _ in range(2):
        with urllib.request.urlopen(url) as response:
            assert response.headers["Content-Type"] == "video/mp4"
            assert len(response.read()) == 100
    assert served["media"].calls == [("clip", 11, 1000, 7300, 640)]
    ranged = urllib.request.Request(url, headers={"Range": "bytes=10-19"})
    with urllib.request.urlopen(ranged) as response:
        assert (response.status, response.read()) == (206, b"0123456789")
        assert response.headers["Content-Range"] == "bytes 10-19/100"
    assert list((served["tmp"] / "explore-data" / "clips").glob("*.mp4"))


def test_a_replaced_file_is_cut_afresh_and_the_stale_url_gets_no_cut(
    served: dict[str, Any],
) -> None:
    old = served["url"] + "/api/search/frame?item_id=imdb:tt1&part=11&t_ms=0"
    new = served["url"] + "/api/search/frame?item_id=imdb:tt1&part=99&t_ms=0"
    urllib.request.urlopen(old).read()
    with open_frames(served["tmp"] / "frames" / "frames.db") as frames:
        frames.execute("UPDATE frame_index_state SET part_id = 99 WHERE item_id = 'imdb:tt1'")
        frames.commit()
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(old)
    assert err.value.code == 404
    urllib.request.urlopen(new).read()
    assert [c[1] for c in served["media"].calls if c[0] == "frames"] == [11, 99]


def test_a_frame_for_a_title_outside_the_index_is_not_found(served: dict[str, Any]) -> None:
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(served["url"] + "/api/search/frame?item_id=imdb:tt3&part=13&t_ms=0")
    assert err.value.code == 404


def test_the_cache_evicts_the_least_recently_used_file_past_its_cap(tmp_path: Path) -> None:
    cache = MediaCache(tmp_path / "clips", cap=250)
    first = cache.get("a.mp4", lambda: b"x" * 100)
    cache.get("b.mp4", lambda: b"x" * 100)
    cache.get("a.mp4", lambda: b"unused")  # a is now newer than b
    assert first.exists()
    cache.get("c.mp4", lambda: b"x" * 100)
    names = sorted(p.name for p in (tmp_path / "clips").iterdir())
    assert names == ["a.mp4", "c.mp4"]


#: Each frame as (item_id, shot in_ms, t_ms, degrees from the phrase). A frame at `d` degrees
#: scores cos(d) with the phrase, and two frames score cos of the difference with each other.
WHY_FRAMES: list[tuple[str, int, int, float]] = [
    ("imdb:tt1", 0, 0, 60),
    ("imdb:tt1", 0, 1000, 20),
    ("imdb:tt1", 9000, 9000, 22),
    ("imdb:tt1", 200000, 200000, 35),
    ("imdb:tt1", 400000, 400000, 80),
    ("imdb:tt1", 600000, 600000, 30),
    ("imdb:tt1", 600000, 610000, 29),
    ("imdb:tt2", 1000, 5000, 21),
    ("imdb:tt2", 1000, 6000, 14),
    ("imdb:tt2", 1000, 7000, 27),
    ("imdb:tt3", 0, 0, 18),
    ("imdb:tt3", 0, 1000, 24),
    ("imdb:tt3", 0, 2000, 12),
]


def _seed_why(world: dict[str, Any], phrases: tuple[str, ...] = ("sunny firefight",)) -> None:
    """Give the Frame index the vectors above and the Phrase cache the phrase's unit vector."""

    def vec(degrees: float) -> str:
        angle = math.radians(degrees)
        return json.dumps([round(127 * math.cos(angle)), round(127 * math.sin(angle))] + [0] * 766)

    with open_frames(world["frames"]) as frames:
        frames.execute(
            "INSERT INTO frame_index_state (item_id, part_id, model, model_version, dims, "
            "quant, indexed_at) VALUES ('imdb:tt3', 13, ?, 'v1', 768, 'int8', ?)",
            (MODEL, NOW),
        )
        starts: dict[str, list[int]] = {}
        for item_id, shot_in, t_ms, degrees in WHY_FRAMES:
            ins = starts.setdefault(item_id, [])
            if shot_in not in ins:
                ins.append(shot_in)
                frames.execute(
                    "INSERT INTO shots (item_id, shot_ix, in_ms, out_ms) VALUES (?, ?, ?, ?)",
                    (item_id, len(ins) - 1, shot_in, shot_in + 5000),
                )
            frames.execute(
                "INSERT INTO frame_vec (item_id, t_ms, shot_ix, vec) VALUES (?, ?, ?, vec_int8(?))",
                (item_id, t_ms, ins.index(shot_in), vec(degrees)),
            )
        frames.commit()
    unit = struct.pack("<768f", 1.0, *([0.0] * 767))
    with sqlite3.connect(world["tmp"] / "phrases.db") as cache:
        cache.execute(
            "CREATE TABLE phrase_cache (phrase TEXT NOT NULL, model TEXT NOT NULL, "
            "model_version TEXT NOT NULL, vec BLOB NOT NULL, created_at TEXT NOT NULL, "
            "last_used_at TEXT NOT NULL, PRIMARY KEY (phrase, model, model_version))"
        )
        for phrase in phrases:
            cache.execute(
                "INSERT INTO phrase_cache VALUES (?, ?, 'v1', ?, ?, ?)",
                (phrase, MODEL, unit, NOW, NOW),
            )


def test_why_scores_the_best_frame_against_the_library_and_names_its_neighbours(
    served: dict[str, Any],
) -> None:
    _seed_why(served)
    status, out = _post(
        served["url"] + "/api/search/why",
        {"phrase": "Sunny  firefight", "item_id": "imdb:tt1", "in_ms": 0},
    )
    assert status == 200
    cosines = [math.cos(math.radians(d)) for *_, d in WHY_FRAMES]
    assert out["frames"] == 13 and out["titles"] == 3
    assert out["best"]["t_ms"] == 1000
    assert out["best"]["sim"] == pytest.approx(math.cos(math.radians(20)), abs=0.01)
    assert out["median"] == pytest.approx(statistics.median(cosines), abs=0.01)
    assert out["highest"] == pytest.approx(max(cosines), abs=0.01)
    assert out["is_highest"] is False
    assert out["top1"] <= out["highest"] and out["top1"] > out["median"]
    assert [p["t_ms"] for p in out["peaks"]] == [1000, 610000, 200000, 400000]
    assert len(out["series"]) == 7
    # The best frame's own Shot (t=0 at 60 degrees) is left out of "in this title".
    assert [e["t_ms"] for e in out["same"]] == [9000, 610000, 600000, 200000, 400000]
    assert out["same"][0]["image_sim"] == pytest.approx(math.cos(math.radians(2)), abs=0.01)
    # Other titles by image similarity, at most two each: the third frame of each is dropped.
    assert [(e["item_id"], e["t_ms"]) for e in out["other"]] == [
        ("imdb:tt2", 5000),
        ("imdb:tt3", 0),
        ("imdb:tt3", 1000),
        ("imdb:tt2", 6000),
    ]
    assert out["other"][0]["title"] == "La Haine" and out["other"][0]["part_id"] == 12
    assert out["title"]["title"] == "Heat" and out["part_id"] == 11


def test_why_says_when_the_frame_is_the_highest_in_the_library(served: dict[str, Any]) -> None:
    _seed_why(served)
    status, out = _post(
        served["url"] + "/api/search/why",
        {"phrase": "sunny firefight", "item_id": "imdb:tt3", "in_ms": 0},
    )
    assert status == 200 and out["is_highest"] is True and out["best"]["t_ms"] == 2000


def test_why_refuses_a_phrase_nobody_searched_and_a_shot_that_is_not_there(
    served: dict[str, Any],
) -> None:
    _seed_why(served)
    status, out = _post(
        served["url"] + "/api/search/why",
        {"phrase": "something else", "item_id": "imdb:tt1", "in_ms": 0},
    )
    assert status == 503 and "Phrase cache" in out["error"]
    status, out = _post(
        served["url"] + "/api/search/why",
        {"phrase": "sunny firefight", "item_id": "imdb:tt1", "in_ms": 123},
    )
    assert status == 404


def test_the_why_column_opens_with_its_sections_and_esc_restores_the_rows(
    served: dict[str, Any], browser: Browser
) -> None:
    _seed_why(served)
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(served["url"] + "/#tab=search")
    page.wait_for_selector("#spoollist li.ptile")
    page.fill("#sphrase", "sunny firefight")
    page.click("#sadvtoggle")
    page.fill("#snot", "gangland execution, futuristic")
    page.click("#ssearch")
    page.wait_for_selector("#srows > li")
    assert page.locator("#srows > li:first-child .thumbs button:visible").count() == 3
    page.click("#srows > li:first-child >> text=Why?")
    page.wait_for_selector("#swhy .wsec", state="attached")
    page.wait_for_function("document.querySelectorAll('#swhy .wsec').length === 4")
    assert "Why #1 matched" in page.inner_text("#swhy")
    assert page.locator("#srows > li.sel").count() == 1
    assert page.locator("#srows > li:first-child .thumbs button:visible").count() == 1
    sections = page.locator("#swhy .wsec h3").all_text_contents()
    assert sections[0] == "Score" and "across the whole film" in sections[1]
    assert sections[2].startswith("Looks most like — in this film")
    assert sections[3].startswith("Looks most like — other titles")
    # The fixture's frame bytes are not a decodable image, so every tile falls back to its caption.
    page.wait_for_selector("#swhy .tile button.missing")
    assert "Heat" in page.locator("#swhy .tile button.missing").first.inner_text()
    page.keyboard.press("Escape")
    assert page.locator("#swhy").is_hidden()
    assert page.locator("#srows > li:first-child .thumbs button:visible").count() == 3
    page.close()


@pytest.fixture
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        chromium = pw.chromium.launch(headless=True)
        try:
            yield chromium
        finally:
            chromium.close()


def test_the_page_draws_token_boxes_results_a_lightbox_and_the_partial_pool_banner(
    served: dict[str, Any], browser: Browser
) -> None:
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url))
    page.goto(served["url"] + "/#tab=search")
    page.wait_for_selector("#searchwrap:not([hidden])")
    page.wait_for_function("document.getElementById('sempty').textContent.length > 0")
    assert "search every indexed title" in page.inner_text("#sempty")
    page.wait_for_selector("#spoollist li.ptile")
    assert page.locator("#spoollist li.ptile").count() == 2

    page.fill("#spoolq", "heist")
    page.click("#spoolsugg li.opt")
    page.wait_for_selector("#spoollist .st.indexed", state="attached")
    page.fill("#spoolq", "ronin")
    page.wait_for_selector("#spoolsugg li.opt")
    page.click("#spoolsugg li.opt")
    page.wait_for_selector("#spoollist .st.none")

    page.fill("#sphrase", "sunny firefight in Los Angeles")
    page.wait_for_function("document.querySelectorAll('#sphrasetok .tok').length === 6")
    assert page.inner_text("#sphrasecount") == "6 / 75"
    page.fill("#sphrase", " ".join(["sunny"] * 76))
    page.wait_for_function("document.getElementById('sphrasecount').textContent === '76 / 75'")
    assert page.locator("#sphrasetok .tok.over").count() == 1
    assert "1 token past the limit" in page.inner_text("#sphrasenote")

    page.fill("#sphrase", "sunny firefight")
    page.click("#sadvtoggle")
    page.fill("#snot", "gangland execution, futuristic, cyberpunk")
    page.wait_for_function("document.getElementById('snotcount').textContent === '2 · 1 · 1'")
    page.click("#ssearch")
    page.wait_for_selector("#srows > li")
    assert page.locator("#srows > li").count() == 3
    assert "Only 2 of 3 titles in this pool are indexed" in page.inner_text("#sbanners")
    assert "Queue the other 1" in page.inner_text("#sbanners")

    page.click("#srows > li:first-child >> text=Frames")
    page.wait_for_selector("#lightbox:not([hidden])")
    assert "frame 1 of 4" in page.inner_text("#lbcap")
    page.keyboard.press("ArrowRight")
    assert "frame 2 of 4" in page.inner_text("#lbcap")
    page.keyboard.press("Escape")
    assert page.locator("#lightbox").is_hidden()

    page.click("#srows > li:first-child >> text=Play")
    # The fixture's clip bytes are not a decodable video, so the page drops the <video> as soon
    # as it errors; the `.clip` box and the Stop label are what stay.
    page.wait_for_selector("#srows > li:first-child .clip")
    page.wait_for_selector("#srows > li:first-child >> text=Stop")
    assert all(r.startswith(served["url"]) or r.startswith("data:") for r in requests), requests
    page.close()


def test_the_sidebar_grid_counts_each_titles_shots_and_narrows_the_results(
    served: dict[str, Any], browser: Browser
) -> None:
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(served["url"] + "/#tab=search")
    page.wait_for_selector("#spoollist li.ptile")
    assert page.locator("#spoollist li.ptile").count() == 2
    assert page.locator("#spoollist .st").all_inner_texts() == ["", ""]
    page.fill("#sphrase", "sunny firefight")
    page.click("#sadvtoggle")
    page.fill("#snot", "gangland execution, futuristic")
    page.click("#ssearch")
    page.wait_for_selector("#srows > li")
    # FOUND ranks Heat twice (ranks 1 and 3) and La Haine once (rank 2).
    counts = page.evaluate(
        "[...document.querySelectorAll('#spoollist li.ptile')]"
        ".map(li => [li.querySelector('.cap').textContent, li.querySelector('.st').textContent])"
    )
    assert counts == [["Heat (1995)", "2 matches"], ["La Haine (1995)", "1 match"]]
    assert page.locator("#srows > li").count() == 3
    page.click("#spoollist li.ptile >> nth=1")
    assert page.locator("#srows > li").count() == 1
    assert page.locator("#spoollist li.sel").count() == 1
    assert page.locator("#sshowall").is_visible()
    page.keyboard.press("Escape")
    assert page.locator("#srows > li").count() == 3
    assert page.locator("#sshowall").is_hidden()
    page.click("#spoollist li.ptile >> nth=0")
    assert page.locator("#srows > li").count() == 2
    page.click("#spoollist li.ptile >> nth=0")
    assert page.locator("#srows > li").count() == 3
    page.click("#spoollist li.ptile >> nth=0")
    page.click("#sshowall")
    assert page.locator("#srows > li").count() == 3
    page.close()


def test_a_phrase_or_negative_that_looks_like_a_flag_is_refused_before_the_reader_runs(
    served: dict[str, Any],
) -> None:
    for body in (
        {"phrase": "--encoder-url", "pool": ["imdb:tt1"]},
        {"phrase": "fire", "pool": ["imdb:tt1"], "negatives": ["--encoder-url", "http://x"]},
    ):
        status, out = _post(served["url"] + "/api/search/run", body)
        assert status == 400 and "--" in out["error"]
    assert not served["log"].exists()


def test_a_clip_window_past_the_title_or_longer_than_two_minutes_is_refused(
    served: dict[str, Any],
) -> None:
    with open_frames(served["frames"]) as frames:
        frames.execute(
            "UPDATE frame_index_state SET duration_ms = 60000 WHERE item_id = 'imdb:tt1'"
        )
        frames.commit()
    base = served["url"] + "/api/search/clip?item_id=imdb:tt1&part=11&in_ms=0&out_ms="
    for out_ms in ("999999999", "61000"):
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(base + out_ms)
        assert err.value.code == 400
    assert served["media"].calls == []


def _rust_call_arguments(text: str, start: int) -> list[str]:
    """The top-level, comma-separated arguments of the call whose `(` is at `start`."""
    depth, args, current, ix, quoted = 0, [], "", start, False
    while ix < len(text):
        char = text[ix]
        if quoted:
            current += char
            if char == "\\":
                ix += 1
                current += text[ix]
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            current += char
        elif char in "([{":
            depth += 1
            if depth > 1:
                current += char
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                args.append(current.strip())
                return [arg for arg in args if arg]
            current += char
        elif char == "," and depth == 1:
            args.append(current.strip())
            current = ""
        else:
            current += char
        ix += 1
    raise AssertionError("a MediaError::new call that never closes")


def _rust_raise_site_reasons() -> list[str]:
    """The `reason` of every `MediaError::new(message, reason)` in the harvester's Rust, with
    each `{}` placeholder of a `format!` reason filled by a number."""
    reasons: list[str] = []
    src = Path(__file__).parent.parent / "crates" / "plexdb" / "src"
    for source in sorted(src.glob("*.rs")):
        text = source.read_text()
        for found in re.finditer(r"MediaError::new\(", text):
            reason = _rust_call_arguments(text, found.end() - 1)[1]
            literal = re.match(r'(?:format!\()?"((?:[^"\\]|\\.)*)"', reason)
            assert literal, f"{source.name}: a reason that is not a string: {reason}"
            reasons.append(re.sub(r"\{[^}]*\}", "9", literal.group(1)))
    return reasons


def _raise_site_reasons() -> list[str]:
    """Every `reason` a `MediaError(...)` in the package's source or a `MediaError::new(...)` in
    the harvester's Rust passes, with each f-string placeholder filled by a number, so a new
    raise site joins the test by being written."""
    reasons: list[str] = _rust_raise_site_reasons()
    for source in sorted(Path(__file__).parent.parent.joinpath("plexdb").glob("*.py")):
        for node in ast.walk(ast.parse(source.read_text())):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "MediaError"
            ):
                continue
            reason = node.args[1]
            if isinstance(reason, ast.Constant):
                reasons.append(str(reason.value))
            else:
                assert isinstance(reason, ast.JoinedStr)
                reasons.append(
                    "".join(
                        "9" if isinstance(part, ast.FormattedValue) else str(part.value)  # type: ignore[attr-defined]
                        for part in reason.values
                    )
                )
    return reasons


def test_the_raise_sites_were_found() -> None:
    assert len(_raise_site_reasons()) >= 19


@pytest.mark.parametrize("reason", sorted(set(_raise_site_reasons())))
def test_the_frame_index_panel_shows_a_failed_titles_reason_and_never_its_error(
    world: dict[str, Any], phrase_tokenizer: PhraseTokenizer, reason: str
) -> None:
    error = f"{world['tmp']}/media/movies/Heat (1995)/Heat.mkv: {reason}: /data/tv/x.mkv"
    with open_frames(world["frames"]) as frames:
        frames.execute(
            "INSERT INTO frame_index_state (item_id, part_id, indexed_at, error, reason) "
            "VALUES ('imdb:tt3', 13, ?, ?, ?)",
            (NOW, error, reason),
        )
        frames.commit()
    tab = SearchTab(
        Config.from_env(), world["store"], world["tmp"] / "explore-data", tokenizer=phrase_tokenizer
    )
    failed: list[dict[str, Any]] = tab.frames_health()["failed"]  # type: ignore[assignment]
    assert [f["reason"] for f in failed] == [reason]
    assert "/" not in json.dumps(failed)
    assert "error" not in failed[0]
    with open_frames(world["frames"]) as frames:
        stored = frames.execute("SELECT error FROM frame_index_state WHERE part_id = 13").fetchone()
    assert stored[0] == error


def test_a_title_that_failed_before_reasons_existed_reads_failed_see_the_log(
    world: dict[str, Any], phrase_tokenizer: PhraseTokenizer
) -> None:
    with open_frames(world["frames"]) as frames:
        frames.execute(
            "INSERT INTO frame_index_state (item_id, part_id, indexed_at, error) "
            "VALUES ('imdb:tt3', 13, ?, '/media/Heat.mkv: ffmpeg exited 1')",
            (NOW,),
        )
        frames.commit()
    tab = SearchTab(
        Config.from_env(), world["store"], world["tmp"] / "explore-data", tokenizer=phrase_tokenizer
    )
    failed: list[dict[str, Any]] = tab.frames_health()["failed"]  # type: ignore[assignment]
    assert [f["reason"] for f in failed] == ["failed (see harvester log)"]


def test_the_frame_index_panel_lists_the_newest_failures_and_counts_the_rest(
    world: dict[str, Any], phrase_tokenizer: PhraseTokenizer
) -> None:
    with open_frames(world["frames"]) as frames:
        for n in range(25):
            frames.execute(
                "INSERT INTO frame_index_state (item_id, part_id, indexed_at, error) "
                "VALUES (?, ?, ?, 'ffmpeg exited 1')",
                (f"imdb:tt{100 + n}", 100 + n, f"2026-10-17T00:{n:02d}:00+00:00"),
            )
        frames.commit()
    tab = SearchTab(
        Config.from_env(), world["store"], world["tmp"] / "explore-data", tokenizer=phrase_tokenizer
    )
    health = tab.frames_health()
    failed: list[dict[str, Any]] = health["failed"]  # type: ignore[assignment]
    assert health["failed_total"] == 25
    assert [f["item_id"] for f in failed] == [f"imdb:tt{124 - n}" for n in range(20)]


def test_the_frame_index_panel_rereads_the_sections_when_the_store_changes(
    world: dict[str, Any], phrase_tokenizer: PhraseTokenizer
) -> None:
    tab = SearchTab(
        Config.from_env(), world["store"], world["tmp"] / "explore-data", tokenizer=phrase_tokenizer
    )
    assert tab.frames_health()["sections"] == []
    with open_store(world["store"]) as conn:
        conn.execute(
            "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
            "VALUES ('1', 'imdb:tt1', '2', ?)",
            (NOW,),
        )
        conn.commit()
    assert tab.frames_health()["sections"] == [
        {"section_id": "2", "indexed": 1, "total": 1, "name": None}
    ]


def _seed_section(world: dict[str, Any]) -> None:
    with open_store(world["store"]) as conn:
        conn.execute(
            "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
            "VALUES ('1', 'imdb:tt1', '2', ?)",
            (NOW,),
        )
        conn.commit()


def test_the_frame_index_bar_names_each_library_by_its_plex_title(
    world: dict[str, Any], phrase_tokenizer: PhraseTokenizer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_section(world)
    monkeypatch.setattr(
        "plexdb.explore_search.LivePlexClient.sections",
        lambda self: [Section(key="2", type="movie", title="Movies")],
    )
    tab = SearchTab(
        Config.from_env(), world["store"], world["tmp"] / "explore-data", tokenizer=phrase_tokenizer
    )
    health = tab.frames_health()
    assert health["sections"] == [{"section_id": "2", "indexed": 1, "total": 1, "name": "Movies"}]
    assert health["names_error"] is None


def test_the_frame_index_bar_keeps_the_section_id_when_plex_does_not_answer(
    world: dict[str, Any], phrase_tokenizer: PhraseTokenizer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_section(world)

    def refuse(self: object) -> list[Section]:
        raise PlexError("cannot reach Plex at http://plex.invalid:32400: refused")

    monkeypatch.setattr("plexdb.explore_search.LivePlexClient.sections", refuse)
    tab = SearchTab(
        Config.from_env(), world["store"], world["tmp"] / "explore-data", tokenizer=phrase_tokenizer
    )
    health = tab.frames_health()
    assert health["sections"] == [{"section_id": "2", "indexed": 1, "total": 1, "name": None}]
    assert health["names_error"] == "Plex did not answer"


def test_the_frame_index_bar_asks_plex_for_names_once_inside_the_cache_window(
    world: dict[str, Any], phrase_tokenizer: PhraseTokenizer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_section(world)
    asked: list[int] = []

    def sections(self: object) -> list[Section]:
        asked.append(1)
        return [Section(key="2", type="movie", title="Movies")]

    monkeypatch.setattr("plexdb.explore_search.LivePlexClient.sections", sections)
    tab = SearchTab(
        Config.from_env(), world["store"], world["tmp"] / "explore-data", tokenizer=phrase_tokenizer
    )
    tab.frames_health()
    tab.frames_health()
    assert len(asked) == 1


def test_an_extra_has_no_plex_part_so_its_frames_are_cut_from_its_own_file(
    served: dict[str, Any],
) -> None:
    with open_store(served["store"]) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('fs:x', 'extra', 'Clip')")
        conn.execute(
            "INSERT INTO extra_library (name, root, type, declared_at) "
            "VALUES ('Music Videos', '/media/mv', 'music-video', ?)",
            (NOW,),
        )
        conn.execute(
            "INSERT INTO extra_item (item_id, library, organized, first_seen) "
            "VALUES ('fs:x', 'Music Videos', 1, ?)",
            (NOW,),
        )
        conn.execute(
            "INSERT INTO extra_file (path, item_id, primary_file, first_seen, last_seen) "
            "VALUES ('/media/mv/clip.mkv', 'fs:x', 1, ?, ?)",
            (NOW, NOW),
        )
        conn.commit()
    with open_frames(served["frames"]) as frames:
        frames.execute(
            "INSERT INTO frame_index_state (item_id, part_id, model, model_version, dims, "
            "quant, indexed_at) VALUES ('fs:x', NULL, ?, 'v1', 768, 'int8', ?)",
            (MODEL, NOW),
        )
        frames.commit()
    tab = served["tab"]
    assert tab._parts(["fs:x", "imdb:tt1"]) == {"fs:x": 0, "imdb:tt1": 11}
    assert tab._extra_path("fs:x") == "/media/mv/clip.mkv"
    urllib.request.urlopen(served["url"] + "/api/search/frame?item_id=fs:x&part=0&t_ms=0").read()
    assert served["media"].calls[-1][:2] == ("frames", 0)
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(served["url"] + "/api/search/frame?item_id=fs:x&part=5&t_ms=0")
    assert err.value.code == 404
    with open_store(served["store"]) as conn:
        conn.execute("UPDATE extra_file SET missing_since = ?", (NOW,))
        conn.commit()
    with pytest.raises(NoFrame):
        tab._extra_path("fs:x")
