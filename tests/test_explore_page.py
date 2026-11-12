"""Plex DB-EX's page (`plexdb/explore.html`) in headless Chromium, served by `plexdb-rs explore`.

Each test starts the Rust server on a fixture store, opens the page with Playwright and reads
what it drew. The server's own routes are tested in `crates/plexdb/tests/explore*.rs`.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import threading
import time
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
from fixture_store import init, refresh_map
from playwright.sync_api import Browser, Page, sync_playwright

from plexdb.frames import migrate, open_frames
from plexdb.store import open_store

FETCHED = "2026-08-09T12:00:00+00:00"

# Four movies and a show. `stinger` rides along with `superhero` on two of the
# three superhero films, which is the shape Plex DB-EX exists to expose.
TITLES = {
    "imdb:tt1": ("movie", "Iron Man", 2008, ["superhero", "stinger", "based on comic"]),
    "imdb:tt2": ("movie", "Thor", 2011, ["superhero", "stinger"]),
    "imdb:tt3": ("movie", "Unbreakable", 2000, ["superhero"]),
    "imdb:tt4": ("movie", "Heat", 1995, ["heist"]),
    "tvdb:9": ("show", "Daredevil", 2015, ["superhero", "lawyer"]),
}


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "plexdb.db"
    init(path)
    with open_store(path) as conn:
        for item_id, (kind, title, year, keywords) in reversed(TITLES.items()):
            conn.execute(
                "INSERT INTO items (item_id, type, title, year) VALUES (?, ?, ?, ?)",
                (item_id, kind, title, year),
            )
            for keyword in reversed(keywords):
                conn.execute(
                    "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
                    "VALUES (?, 'keywords', 'tmdb', 'keyword', ?, ?)",
                    (item_id, keyword, FETCHED),
                )
        # A movie with no keywords is not part of N, just as a pool candidate
        # with no facts is not part of taste-cosine's doc_count.
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('fs:x', 'movie', 'Bare')")
        conn.commit()
    return path


def _explore(store: Path, env: dict[str, str] | None = None) -> Iterator[str]:
    """Run `plexdb-rs explore` on `store` at a free port and yield its base URL."""
    binary = os.environ.get("PLEXDB_RS_BIN", "").strip() or "plexdb-rs"
    child_env = {k: v for k, v in os.environ.items() if k != "PLEXDB_EXPLORE_SAVED_PATH"}
    child_env.update(env or {})
    child = subprocess.Popen(
        [binary, "explore", "--host", "127.0.0.1", "--port", "0", "--store", str(store)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=child_env,
    )
    assert child.stdout is not None
    try:
        line = child.stdout.readline()
        assert line, f"plexdb-rs explore exited with {child.poll()} before it printed its address"
        port = line.split("http://127.0.0.1:", 1)[1].split("/", 1)[0]
        yield f"http://127.0.0.1:{port}"
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        child.stdout.close()


@pytest.fixture
def base_url(store: Path) -> Iterator[str]:
    yield from _explore(store)


@pytest.fixture
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        chromium = pw.chromium.launch(headless=True)
        try:
            yield chromium
        finally:
            chromium.close()


def _series_key(panel: dict[str, Any]) -> str:
    """`<panel id>:<first 12 hex of sha256(sql NUL value)>`, the name of the panel's series."""
    digest = hashlib.sha256(f"{panel['sql']}\0{panel['value']}".encode()).hexdigest()
    return f"{panel['id']}:{digest[:12]}"


def _seeded_panels(base_url: str, store: Path) -> list[dict[str, Any]]:
    """The first dashboard's panels as the server seeded them into `explore-panels.json`."""
    urllib.request.urlopen(f"{base_url}/api/dashboards").read()
    seeded = json.loads((store.parent / "explore-panels.json").read_text())
    return cast("list[dict[str, Any]]", seeded["dashboards"][0]["panels"])


# ---- ?clip and ?demo in a headless browser ----
# The recorder contract in docs/explore.md, read off the page with Playwright. Only `superhero`
# of the three demo tags is on this store's movie map, so the period here is 2.5 s, not 4.5 s.

CLIP_READY = "() => window.__clip || document.body.dataset.demo === 'failed'"
# ?demo exposes no seam; its caption names a tag once the live tour has reached the first one.
DEMO_READY = (
    "() => document.getElementById('democap').childElementCount"
    " || document.body.dataset.demo === 'failed'"
)
CLIP_FRAME = "() => ({ k: mapVP.k, tx: mapVP.tx, ty: mapVP.ty, tag: state.tag })"
# The fit the viewport should draw at zoom 1, worked out from the canvas size and the map bounds.
CLIP_FIT = """() => {
  const c = document.getElementById("map"), b = mapVP.bounds, pad = mapVP.pad;
  return { k: mapVP.k, fit: Math.min((c.clientWidth - 2 * pad) / (b.x1 - b.x0),
                                     (c.clientHeight - 2 * pad) / (b.y1 - b.y0)) };
}"""


def _open_clip(browser: Browser, store: Path, url: str, ready: str = CLIP_READY) -> Page:
    refresh_map(store)
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(url)
    page.wait_for_function(ready, timeout=60_000)
    assert page.evaluate("() => document.body.dataset.demo") != "failed", page.evaluate(
        "() => document.getElementById('error').textContent"
    )
    return page


def _seek(page: Page, t: float) -> dict[str, Any]:
    page.evaluate("t => window.__clip.seek(t)", t)
    return dict(page.evaluate(CLIP_FRAME))


def test_a_line_panel_whose_every_night_is_zero_still_draws_its_point(
    browser: Browser, base_url: str, store: Path
) -> None:
    damaged = _seeded_panels(base_url, store)[0]
    (store.parent / "panel-history.json").write_text(
        json.dumps({"series": {_series_key(damaged): [{"day": "2026-10-21", "value": 0}]}})
    )
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(base_url)
    page.click('button[data-tab="query"]')
    page.wait_for_selector("circle.dot")

    assert page.get_attribute("circle.dot", "cy") != "NaN"
    assert page.locator("svg text", has_text="NaN").count() == 0


def test_the_dashboard_draws_the_five_seeded_panels_and_edit_opens_the_editor(
    browser: Browser, base_url: str
) -> None:
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(base_url)
    page.click('button[data-tab="query"]')
    page.wait_for_selector(".tile")

    assert page.locator(".tile h3").all_inner_texts() == [
        "Damaged or rule-breaking files",
        "English original, no English subtitle",
        "Decode progress",
        "Tracks per language",
        "Extras with more than one file",
    ]
    assert page.locator("#dashpick option:checked").inner_text() == "Library health"
    assert page.inner_text("#qtitle .sub") == "5 panels \u00b7 recorded nightly by the sweep"
    assert page.is_hidden("#sql")

    page.locator(".tile button", has_text="Edit").nth(2).click()

    assert page.is_visible("#sql") and page.is_hidden("#dashwrap")
    assert "parts_decoded" in page.input_value("#sql")


def test_the_dashboard_shows_a_loading_line_until_the_request_answers(
    browser: Browser, base_url: str
) -> None:
    held: dict[str, Any] = {}
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.route(lambda u: "/api/dashboards" in u, lambda r: held.update(req=r))
    page.goto(base_url)
    page.click('button[data-tab="query"]')
    page.wait_for_selector("#dashloading")

    assert page.inner_text("#dashloading") == "Loading dashboards…"
    assert page.locator(".tile").count() == 0

    held.pop("req").continue_()
    page.wait_for_selector(".tile")
    assert page.locator("#dashloading").count() == 0


_TRACKS = (
    "SELECT 'eng' AS lang, 'audio' AS kind, 10 AS n UNION ALL SELECT 'eng', 'subtitle', 9 "
    "UNION ALL SELECT 'jpn', 'audio', 4 UNION ALL SELECT 'fre', 'audio', 3 "
    "UNION ALL SELECT 'spa', 'audio', 2 UNION ALL SELECT 'ger', 'audio', 1 "
    "UNION ALL SELECT 'ita', 'audio', 1 UNION ALL SELECT 'por', 'audio', 1"
)
_TRACKS_BY_LANGUAGE = (
    f"SELECT lang, SUM(n) AS n FROM ({_TRACKS}) GROUP BY lang ORDER BY n DESC, lang"
)
_COLUMNS = {"label": "lang", "number": "n", "x": "kind", "y": "lang"}


def _chart_panels(sql: dict[str, str], value: str = "rows") -> list[dict[str, Any]]:
    return [
        {
            "id": chart,
            "title": chart,
            "chart": chart,
            "sql": sql.get(chart, sql["*"]),
            "value": value,
            "width": 1,
            "height": "short",
            **({k: v for k, v in _COLUMNS.items() if chart != "stat"}),
        }
        for chart in ("line", "stat", "bar", "pie", "table", "heatmap")
    ]


def _open_dashboard(
    browser: Browser, base_url: str, store: Path, panels: list[dict[str, Any]]
) -> Page:
    (store.parent / "explore-panels.json").write_text(
        json.dumps({"dashboards": [{"id": "d", "name": "D", "panels": panels}]})
    )
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(base_url)
    page.click('button[data-tab="query"]')
    page.wait_for_selector(".tile")
    return page


def test_a_bar_panel_caps_at_its_tiles_rows_and_sums_the_rest_into_other(
    browser: Browser, base_url: str, store: Path
) -> None:
    def bars(rows: int, height: str) -> tuple[list[str], list[str]]:
        sql = " UNION ALL ".join(f"SELECT 'l{i:02}' AS lang, {i} AS n" for i in range(1, rows + 1))
        panel = _chart_panels({"*": sql})[2] | {"height": height}
        page = _open_dashboard(browser, base_url, store, [panel])
        box = page.locator(".bars")
        out = (box.locator(".bl").all_inner_texts(), box.locator(".bv").all_inner_texts())
        page.close()
        return out

    names, values = bars(20, "tall")
    assert names == [f"l{i:02}" for i in range(20, 12, -1)] + ["other"]
    assert values[-1] == str(sum(range(1, 13)))
    names, values = bars(20, "short")
    assert names == ["l20", "l19", "l18", "l17", "other"]
    assert values[-1] == str(sum(range(1, 17)))
    names, _ = bars(3, "tall")
    assert names == ["l03", "l02", "l01"]


def test_pie_table_and_heatmap_draw_their_rows_and_stat_draws_a_sparkline(
    browser: Browser, base_url: str, store: Path
) -> None:
    panels = _chart_panels({"*": _TRACKS, "pie": _TRACKS_BY_LANGUAGE})
    path = store.parent / "explore-panels.json"
    path.write_text(json.dumps({"dashboards": [{"id": "d", "name": "D", "panels": panels}]}))
    stat = next(p for p in panels if p["chart"] == "stat")
    (store.parent / "panel-history.json").write_text(
        json.dumps(
            {
                "series": {
                    _series_key(stat): [
                        {"day": "2026-10-20", "value": 5},
                        {"day": "2026-10-21", "value": 8},
                    ]
                }
            }
        )
    )
    page = _open_dashboard(browser, base_url, store, panels)

    def tile(title: str) -> Any:
        return page.locator(".tile", has=page.locator("h3", has_text=title))

    assert tile("stat").locator("svg polyline.spark").count() == 1
    assert "+3 since Oct 20" in tile("stat").inner_text()
    legend = tile("pie").locator(".pielegend")
    assert legend.locator("span:not(.n):not(.pc)").all_inner_texts() == [
        "eng", "jpn", "fre", "spa", "ger", "other",
    ]  # fmt: skip
    assert legend.locator(".n").all_inner_texts() == ["19", "4", "3", "2", "1", "2"]
    assert legend.locator(".pc").all_inner_texts() == ["61%", "13%", "10%", "6%", "3%", "6%"]
    assert tile("pie").locator("svg path").count() == 6
    for width in (1280, 1000, 800):
        page.set_viewport_size({"width": width, "height": 800})
        pie = tile("pie").locator(".pie svg").bounding_box()
        side = legend.bounding_box()
        assert pie is not None and side is not None
        assert side["x"] >= pie["x"] + pie["width"]  # the legend sits beside the pie
        assert abs((side["y"] + side["height"] / 2) - (pie["y"] + pie["height"] / 2)) < 2
        tall = max(
            (tile(t).bounding_box() or {"height": 0})["height"] for t in ("bar", "heatmap", "table")
        )
        assert (tile("pie").bounding_box() or {"height": 999})["height"] <= tall
    page.close()

    big = (
        "SELECT 'mkv' AS lang, 777512 AS n UNION ALL SELECT 'mp4', 86690 "
        "UNION ALL SELECT 'avi', 9680 UNION ALL SELECT '(none)', 7000 "
        "UNION ALL SELECT 'mpegts', 60000 UNION ALL SELECT 'a-very-long-container-name', 5"
    )
    page = _open_dashboard(browser, base_url, store, _chart_panels({"*": big}))
    for width in (1280, 1000):
        page.set_viewport_size({"width": width, "height": 800})
        box = tile("pie").bounding_box()
        assert box is not None
        cells = (
            tile("pie")
            .locator(".pielegend > *")
            .evaluate_all(
                "els => els.map(e => { const r = e.getBoundingClientRect();"
                " return [r.right, Math.round(r.top + r.height / 2)]; })"
            )
        )
        assert len(cells) == 24
        assert all(right <= box["x"] + box["width"] for right, _ in cells)
        assert len({mid for _, mid in cells}) == 6  # a row's four cells share a line
    page.close()
    page = _open_dashboard(browser, base_url, store, panels)
    assert tile("table").locator("th").all_inner_texts() == ["lang", "kind", "n"]
    assert tile("table").locator("tbody tr").count() == 8
    heat = tile("heatmap")
    assert heat.locator(".hx").all_inner_texts() == ["audio", "subtitle"]
    assert heat.locator(".hy").all_inner_texts() == [
        "eng",
        "jpn",
        "fre",
        "spa",
        "ger",
        "ita",
        "por",
    ]
    assert heat.locator(".hc").first.inner_text() == "10"
    assert heat.locator(".hc.none").count() == 6
    assert heat.locator(".hc.hot").count() == 2


def test_a_table_panel_over_more_than_50_rows_says_how_many_it_shows(
    browser: Browser, base_url: str, store: Path
) -> None:
    def note(rows: int) -> tuple[int, str]:
        sql = (
            "WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s "
            f"WHERE n < {rows}) SELECT n AS lang, n AS kind, n FROM s"
        )
        page = _open_dashboard(browser, base_url, store, [_chart_panels({"*": sql})[4]])
        tile = page.locator(".tile")
        out = (tile.locator("tbody tr").count(), tile.locator(".tilenote").all_inner_texts())
        page.close()
        return out[0], " ".join(out[1])

    assert note(120) == (50, "First 50 of 120 rows")
    assert note(50) == (50, "")
    assert note(600) == (50, "First 50 of 500+ rows")


def test_a_panel_of_every_type_with_no_rows_says_so(
    browser: Browser, base_url: str, store: Path
) -> None:
    empty = f"SELECT * FROM ({_TRACKS}) WHERE n < 0"
    panels = _chart_panels({"*": empty}, value="column:n")
    panels.append(
        {
            "id": "gauge",
            "title": "gauge",
            "chart": "gauge",
            "sql": empty,
            "value": "column:n",
            "total": "n",
            "width": 1,
            "height": "short",
        }
    )
    page = _open_dashboard(browser, base_url, store, panels)

    assert page.locator(".tile").count() == 7
    assert page.locator(".tile .tilenote", has_text="No rows tonight.").count() == 7


def test_clip_seek_draws_the_same_frame_at_t_and_t_plus_period(
    browser: Browser, store: Path, base_url: str
) -> None:
    page = _open_clip(browser, store, f"{base_url}/?clip")
    period = page.evaluate("() => window.__clip.period")
    frames = {t: _seek(page, t) for t in (0, 0.7, 1.0, 1.9, 2.2)}
    for t, frame in frames.items():
        assert _seek(page, t + period) == frame, t
    # A tag is lit and the camera moves somewhere in the loop, so the seam compares real frames.
    assert {f["tag"] for f in frames.values()} > {None}
    assert len({f["k"] for f in frames.values()}) > 1


def test_clip_seek_after_a_resize_fits_the_whole_map_to_the_new_canvas(
    browser: Browser, store: Path, base_url: str
) -> None:
    page = _open_clip(browser, store, f"{base_url}/?clip")
    _seek(page, 1.0)  # a zoomed frame, so the viewport is no longer fitted when the resize lands
    # 900x600 seeks before the ResizeObserver can run; 1600x500 seeks after it has.
    for width, height, settle in ((900, 600, False), (1600, 500, True)):
        page.set_viewport_size({"width": width, "height": height})
        if settle:
            page.evaluate(
                "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
            )
        _seek(page, 0)
        drawn = page.evaluate(CLIP_FIT)
        assert drawn["k"] == pytest.approx(drawn["fit"], rel=1e-9), (width, height)


def test_clip_forces_the_tags_tab_over_the_hash(
    browser: Browser, store: Path, base_url: str
) -> None:
    page = _open_clip(browser, store, f"{base_url}/?clip#tab=query")
    assert page.evaluate("() => state.tab") == "tags"
    assert page.evaluate("() => document.getElementById('tagsmain').hidden") is False


def test_demo_exposes_no_clip_seam(browser: Browser, store: Path, base_url: str) -> None:
    page = _open_clip(browser, store, f"{base_url}/?demo", ready=DEMO_READY)
    assert page.evaluate("() => window.__clip") is None


def _pin_while_the_map_redraws(browser: Browser, store: Path, base_url: str, pin: str) -> Page:
    """Run `pin` on the dimmed default map, then land the redraw without the noise tags
    before the title's details: the order that swaps every point under a pin in flight."""
    refresh_map(store)
    held: dict[str, Any] = {}
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.add_init_script("localStorage.setItem('plexdb-explore:noise', '[\"stinger\"]')")
    page.route(lambda u: "/api/map?" in u and "exclude=" in u, lambda r: held.update(map=r))
    page.route(lambda u: "/api/title?" in u, lambda r: held.update(title=r))
    page.goto(f"{base_url}/#kind=movie&view=map")
    page.wait_for_function("() => map.dim && map.byId.has('imdb:tt1')", timeout=30_000)
    page.evaluate(pin)
    deadline = time.monotonic() + 10
    while "title" not in held:
        assert time.monotonic() < deadline, "the pin never asked for the title's details"
        page.wait_for_timeout(20)
    held.pop("map").continue_()
    page.wait_for_function("() => !map.dim", timeout=30_000)
    held.pop("title").continue_()
    page.wait_for_function("() => document.querySelector('#rightcol .tcard ol')", timeout=30_000)
    return page


def test_a_title_pinned_while_the_map_redraws_keeps_its_card_and_nearest_titles(
    browser: Browser, store: Path, base_url: str
) -> None:
    page = _pin_while_the_map_redraws(
        browser, store, base_url, "() => mapVP.onSelect(map.byId.get('imdb:tt1'))"
    )
    pin = page.evaluate(
        """() => ({ id: mapVP.selected && mapVP.selected.id,
                    onMap: mapVP.selected === map.byId.get('imdb:tt1'),
                    open: !pinCard.root.hidden, similar: mapUI.similar })"""
    )
    assert pin["id"] == "imdb:tt1" and pin["onMap"] and pin["open"], pin
    assert "imdb:tt2" in pin["similar"], pin


def test_a_title_searched_while_the_map_redraws_pins_its_point_on_the_new_map(
    browser: Browser, store: Path, base_url: str
) -> None:
    page = _pin_while_the_map_redraws(
        browser,
        store,
        base_url,
        "() => { chooseSuggestion(suggestions('iron man')[0]); }",
    )
    page.wait_for_function("() => mapUI.search && mapVP.selected", timeout=10_000)
    pin = page.evaluate(
        """() => ({ id: mapVP.selected.id, onMap: mapVP.selected === map.byId.get('imdb:tt1'),
                    focus: mapUI.search.focus })"""
    )
    assert pin == {"id": "imdb:tt1", "onMap": True, "focus": "imdb:tt1"}, pin


def test_a_redraw_under_a_zoomed_pin_keeps_the_zoom_and_centres_the_pin_beside_its_card(
    browser: Browser, store: Path, base_url: str
) -> None:
    page = _pin_while_the_map_redraws(
        browser,
        store,
        base_url,
        "() => { mapVP.onSelect(map.byId.get('imdb:tt1')); mapVP.zoomAt(0, 0, 8); }",
    )
    view = page.evaluate(
        """() => { const it = mapVP.selected, pad = clearOfPinCard();
                   return { zoom: mapVP.zoom, fitted: mapVP.fitted,
                            x: mapVP.sx(it.x), y: mapVP.sy(it.y),
                            cx: (pad.l + mapVP.w - pad.r) / 2,
                            cy: (pad.t + mapVP.h - pad.b) / 2 }; }"""
    )
    assert view["zoom"] == pytest.approx(8) and not view["fitted"], view
    assert (view["x"], view["y"]) == pytest.approx((view["cx"], view["cy"]), abs=1), view


def test_merges_jev_only_filter_lists_jev_calls_and_no_rule_calls(
    browser: Browser, store: Path, base_url: str
) -> None:
    with open_store(store) as conn:
        conn.executemany(
            "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_score, jev_model, judged_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("stinger", "superhero", 0.95, "jev-1.13.0", FETCHED),
                ("heist", "lawyer", 1.0, "rule:film-suffix", FETCHED),
            ],
        )
        conn.commit()
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(f"{base_url}/#tab=merges")
    page.wait_for_function(
        "() => document.querySelectorAll('#mrows li .badge').length", timeout=30_000
    )
    badges = "els => els.map(e => e.textContent)"
    every = page.eval_on_selector_all("#mrows li .badge", badges)
    assert sorted(every) == ["Jev ≥ 0.9", "film-suffix rule"], every
    page.click("button[data-mshow='jev']")
    jev = page.eval_on_selector_all("#mrows li .badge", badges)
    assert jev == ["Jev ≥ 0.9"], jev


def test_demo_moves_the_camera_without_a_seek(browser: Browser, store: Path, base_url: str) -> None:
    page = _open_clip(browser, store, f"{base_url}/?demo", ready=DEMO_READY)
    # One second of samples, longer than the 0.6 s the camera holds still on a tag.
    xs = page.evaluate(
        """() => new Promise(r => {
          const xs = [], t0 = performance.now();
          const take = () => { xs.push(mapVP.tx);
                               performance.now() - t0 < 1000 ? setTimeout(take, 50) : r(xs); };
          take();
        })"""
    )
    assert len(set(xs)) > 1, xs


# (shared, N, df_anchor, df_other, z) -> cautious lift, for heist on the 2026-10 movie store
# (184 of 11,533 titles): jewelry heist (8 shared, on 9), a 2-title pair (on 2), and heist and
# thrilling action (92 shared, on 297).
CAUTIOUS_CASES = [
    ((8, 11533, 184, 9, 0.0), 8 * 11533 / (184 * 9)),
    ((8, 11533, 184, 9, 2.0), 28.063),
    ((2, 11533, 184, 2, 2.0), 16.823),
    ((2, 11533, 184, 2, 4.0), 6.338),
    ((92, 11533, 184, 297, 4.0), 13.924),
]


def _add_superhero_film(store: Path) -> None:
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt5', 'movie', 'Hancock')"
        )
        for keyword in ("superhero", "stinger"):
            conn.execute(
                "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
                "VALUES ('imdb:tt5', 'keywords', 'tmdb', 'keyword', ?, ?)",
                (keyword, FETCHED),
            )
        conn.commit()


def test_the_page_refetches_tags_when_cotags_come_from_a_newer_snapshot(
    browser: Browser, store: Path, base_url: str
) -> None:
    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_function("() => state.data && state.data.stamp")
    old = page.evaluate("state.data.stamp")
    assert page.evaluate("state.data.titles") == 4

    # The snapshot is replaced between the page's /api/tags and its /api/cotags.
    _add_superhero_film(store)
    page.evaluate("state.tag = 'superhero'; loadCotags('superhero')")
    page.wait_for_function("old => state.data.stamp !== old", arg=old)
    page.wait_for_function(
        "() => cotags.data && cotags.data.stamp === state.data.stamp && coLive() !== null"
    )
    assert page.evaluate("state.data.titles") == 5
    assert page.evaluate("cotags.data.titles") == 5
    page.close()


def test_cautious_lift_marks_down_few_titles_and_the_page_agrees(
    browser: Browser, base_url: str
) -> None:
    """At z = 0 it is the plain lift; more evidence shrinks a 2-title lift far more than an
    8- or 92-title one. The page's evidence slider computes it in `cautiousLift`."""
    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_function("() => typeof cautiousLift === 'function'")
    for args, expected in CAUTIOUS_CASES:
        js = page.evaluate("a => cautiousLift(...a)", list(args))
        assert js == pytest.approx(expected, abs=1e-3), args


def _related_note(page: Page, df: int, co: list[list[object]]) -> str | None:
    """The caveat text the Related tags tab shows for a tag on `df` titles whose co-occurrence
    is `co` (`[tag, shared, df]` rows), or None when it shows none."""
    return cast(
        "str | None",
        page.evaluate(
            """([df, co]) => {
          showTag("probe");
          Object.assign(cotags, { tag: "probe", kind: state.kind, token: cotags.token + 1,
            data: { tag: "probe", df, titles: 1000, co, stamp: state.data && state.data.stamp } });
          paintCotags();
          const n = document.querySelector("#cobox .cothin");
          return n ? n.textContent : null;
        }""",
            [df, co],
        ),
    )


def test_related_tags_say_when_the_top_pairings_rest_on_a_few_titles(
    browser: Browser, base_url: str
) -> None:
    """A tag on 31 titles whose best pairings share 1-2 titles gets a caveat naming the count;
    a tag whose best pairings share 10 or more gets none."""
    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_function("() => typeof showTag === 'function'")
    small = [[f"rare{i}", 1 + i % 2, 1 + i % 2] for i in range(12)]
    note = _related_note(page, 31, small)
    assert note is not None
    assert "1\u20132" in note and "(probe is on 31" in note, note
    big = [[f"solid{i}", 10 + i, 12 + i] for i in range(12)]
    assert _related_note(page, 184, big) is None
    # The rule's edges: shared 3 is thin, shared 4 is not.
    assert _related_note(page, 40, [[f"t{i}", 3, 3] for i in range(12)]) is not None
    assert _related_note(page, 40, [[f"t{i}", 4, 4] for i in range(12)]) is None


# ---- the Lists tab ----


def _add_lists(store: Path) -> None:
    """Two lists: one MDBList list holding Iron Man (owned) and a film the library lacks, and
    an empty Letterboxd one."""
    with open_store(store) as conn:
        conn.executemany(
            "INSERT INTO collection (collection_id, source, name, url, size, likes, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "mdblist:1",
                    "mdblist",
                    "Mindfuck Movies",
                    "https://mdblist.com/lists/lina/mf",
                    5,
                    9,
                    FETCHED,
                ),
                (
                    "letterboxd:2",
                    "letterboxd",
                    "Heist Night",
                    "https://letterboxd.com/x/list/h/",
                    2,
                    3,
                    FETCHED,
                ),
            ],
        )
        conn.execute(
            "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
            "VALUES ('imdb:tt1', 'imdb', 'tt1', 'movie', ?)",
            (FETCHED,),
        )
        conn.executemany(
            "INSERT INTO collection_entry (collection_id, rank, ns, value, kind, title, year) "
            "VALUES ('mdblist:1', ?, 'imdb', ?, 'movie', ?, ?)",
            [(1, "tt1", "Iron Man", 2008), (2, "tt99", "Primer", 2004)],
        )
        conn.commit()


def test_lists_tab_selects_clears_and_makes_no_outside_request(
    browser: Browser, store: Path, base_url: str
) -> None:
    _add_lists(store)
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    outside: list[str] = []
    page.on(
        "request",
        lambda r: (
            outside.append(r.url)
            if not r.url.startswith(base_url) and not r.url.startswith("data:")
            else None
        ),
    )
    page.goto(f"{base_url}/#tab=lists")
    page.wait_for_selector("#ltable tbody tr")
    assert page.locator("#ltable tbody tr").count() == 2
    page.locator("#ltable tbody tr", has_text="Mindfuck Movies").click()
    page.wait_for_selector("#linspect .lentries li")
    assert page.locator("#linspect .lentries li").all_text_contents() == [
        "1Iron Man (2008)",
        "2Primer (2004)",
    ]
    assert page.locator("#linspect .lentries li.unowned").count() == 1
    page.locator("#ltable tbody tr", has_text="Mindfuck Movies").click()
    assert page.locator("#ltable tr.selected").count() == 0
    page.locator("#ltable tbody tr", has_text="Mindfuck Movies").click()
    page.wait_for_selector("#linspect .lentries li")
    srcs = page.eval_on_selector_all("#ltable img", "els => els.map(e => e.src.slice(0, 22))")
    assert srcs == ["data:image/png;base64,"] * 2, srcs
    page.keyboard.press("Escape")
    assert page.locator("#ltable tr.selected").count() == 0
    assert "Select a list" in page.locator("#linspect").inner_text()
    page.set_viewport_size({"width": 720, "height": 800})
    page.locator("#ltable tbody tr", has_text="Mindfuck Movies").click()
    page.wait_for_selector("#lback")
    assert not page.locator("#ltable").is_visible()
    page.click("#lback")
    assert page.locator("#ltable").is_visible()
    assert outside == []
    page.close()


def test_lists_tab_names_the_empty_state(browser: Browser, store: Path, base_url: str) -> None:
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(f"{base_url}/#tab=lists")
    page.wait_for_selector("#lempty:not([hidden])")
    assert "No lists stored yet" in page.locator("#lempty").inner_text()
    page.close()


def test_lists_tab_adds_excludes_and_shows_a_failed_list(
    browser: Browser, store: Path, base_url: str
) -> None:
    _add_lists(store)
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO list_attempt (list_key, attempts, attempted_at, reason) VALUES "
            "('https://mdblist.com/lists/pierce/gone', 3, '2026-10-14T03:30:00+00:00', "
            "'MDBList returned 404')"
        )
        conn.execute(
            "INSERT INTO list_decision (url, decision, decided_at, added) VALUES "
            "('https://mdblist.com/lists/pierce/gone', 'added', '2026-10-13T00:00:00+00:00', 1)"
        )
        conn.commit()
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(f"{base_url}/#tab=lists")
    page.wait_for_selector("#ltable tbody tr")

    page.fill("#laddurl", "https://mdblist.com/lists/pierce/new")
    page.click("#ladd")
    # The page clears the box and writes the status only after it has repainted the table, and
    # the failed list is already waiting, so wait on that signal rather than on any waiting row.
    page.wait_for_function("document.querySelector('#laddurl').value === ''")
    assert "The next sweep fetches it" in page.locator("#laddstatus").inner_text()
    added = page.locator("#ltable tr.waiting", has_text="pierce/new")
    assert "Waiting for sweep" in added.inner_text()

    page.fill("#laddurl", "https://example.com/x")
    page.click("#ladd")
    page.wait_for_function("document.querySelector('#laddstatus').classList.contains('bad')")

    page.locator("#ltable tbody tr", has_text="pierce/gone").click()
    inspector = page.locator("#linspect")
    inspector.get_by_text("Fetch failed 3 times in a row").wait_for()
    assert "MDBList returned 404" in inspector.inner_text()
    assert "stopped trying until you retry" in inspector.inner_text()
    assert inspector.get_by_role("button").all_text_contents().count("Retry") == 1
    inspector.get_by_role("button", name="Retry").click()
    page.wait_for_selector("#linspect .lpending")
    assert "Waiting for sweep" in inspector.inner_text()

    page.locator("#ltable tbody tr", has_text="Mindfuck Movies").click()
    page.wait_for_selector("#linspect .lentries li")
    inspector.get_by_role("button", name="Exclude").click()
    page.wait_for_selector("#linspect .lpending")
    assert "Excluded · next sweep removes it" in inspector.inner_text()
    assert inspector.get_by_role("button", name="Retry").count() == 0
    page.close()


# ---- the Search tab ----

MODEL = "test/clip"
NOW = "2026-10-16T00:00:00+00:00"
FRAME_DIMS = 768
TOKENIZER = Path(__file__).parent / "fixtures" / "search_tokenizer.json"

# (item_id, shot in_ms, t_ms, degrees from the phrase). The fake encoder embeds every phrase as
# the unit vector along axis 0, so a frame scores cos(degrees) with any phrase.
SEARCH_FRAMES = [
    ("imdb:tt1", 0, 0, 60.0),
    ("imdb:tt1", 0, 1000, 20.0),
    ("imdb:tt1", 0, 2000, 40.0),
    ("imdb:tt1", 0, 3000, 50.0),
    ("imdb:tt1", 9000, 9000, 22.0),
    ("imdb:tt1", 9000, 10000, 70.0),
    ("imdb:tt2", 1000, 1000, 30.0),
    ("imdb:tt2", 1000, 2000, 45.0),
    ("imdb:tt2", 1000, 3000, 60.0),
]


def _json_server(answer: Any) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self._reply(answer(self.path, json.loads(self.rfile.read(length) or b"null")))

        def do_GET(self) -> None:
            self._reply(answer(self.path, None))

        def _reply(self, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def _encoder_answer(_path: str, body: Any) -> dict[str, Any]:
    unit = [1.0] + [0.0] * (FRAME_DIMS - 1)
    return {"data": [{"index": i, "embedding": unit} for i in range(len(body["input"]))]}


def _plex_answer(path: str, _body: Any) -> dict[str, Any]:
    if path.startswith("/library/sections"):
        return {"MediaContainer": {"Directory": [{"key": "2", "type": "movie", "title": "Movies"}]}}
    return {"MediaContainer": {"Metadata": []}}


@pytest.fixture
def search_url(tmp_path: Path) -> Iterator[str]:
    store = tmp_path / "plexdb.db"
    init(store)
    with open_store(store) as conn:
        for item_id, title, year, key in (
            ("imdb:tt1", "Heat", 1995, "101"),
            ("imdb:tt2", "La Haine", 1995, "102"),
            ("imdb:tt3", "Ronin", 1998, "103"),
        ):
            conn.execute(
                "INSERT INTO items (item_id, type, title, year) VALUES (?, 'movie', ?, ?)",
                (item_id, title, year),
            )
            conn.execute(
                "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
                "VALUES (?, ?, '2', ?)",
                (key, item_id, NOW),
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
        for item_id, part in (("imdb:tt1", 11), ("imdb:tt2", 12)):
            frames.execute(
                "INSERT INTO frame_index_state (item_id, part_id, model, model_version, dims, "
                "quant, duration_ms, vectors, indexed_at) "
                "VALUES (?, ?, ?, 'v1', ?, 'int8', 20000, 3, ?)",
                (item_id, part, MODEL, FRAME_DIMS, NOW),
            )
        starts: dict[str, list[int]] = {}
        for item_id, shot_in, t_ms, degrees in SEARCH_FRAMES:
            ins = starts.setdefault(item_id, [])
            if shot_in not in ins:
                ins.append(shot_in)
                frames.execute(
                    "INSERT INTO shots (item_id, shot_ix, in_ms, out_ms) VALUES (?, ?, ?, ?)",
                    (item_id, len(ins) - 1, shot_in, shot_in + 5000),
                )
            angle = math.radians(degrees)
            vec = [round(127 * math.cos(angle)), round(127 * math.sin(angle))]
            frames.execute(
                "INSERT INTO frame_vec (item_id, t_ms, shot_ix, vec) VALUES (?, ?, ?, vec_int8(?))",
                (item_id, t_ms, ins.index(shot_in), json.dumps(vec + [0] * (FRAME_DIMS - 2))),
            )
        frames.commit()
    encoder, encoder_url = _json_server(_encoder_answer)
    plex, plex_url = _json_server(_plex_answer)
    env = {
        "PLEXDB_EXPLORE_SAVED_PATH": str(tmp_path / "explore-data" / "explore-queries.json"),
        "PLEXDB_FRAMES_DIR": str(frames_dir),
        "PLEXDB_PHRASE_CACHE": str(tmp_path / "phrases.db"),
        "PLEXDB_FRAME_ENCODER_URL": encoder_url,
        "PLEXDB_FRAME_MODEL": MODEL,
        "PLEXDB_FRAME_TOKENIZER": str(TOKENIZER),
        "PLEXDB_MEDIA_DIR": str(tmp_path / "media"),
        "PLEX_URL": plex_url,
        "PLEX_TOKEN": "secret",
        "PLEX_SOURCE_ROOTS": "/library",
    }
    (tmp_path / "explore-data").mkdir()
    try:
        yield from _explore(store, env)
    finally:
        for server in (encoder, plex):
            server.shutdown()
            server.server_close()


def _search(page: Page) -> None:
    page.fill("#sphrase", "sunny firefight")
    page.click("#sadvtoggle")
    page.fill("#snot", "gangland execution, futuristic")
    page.click("#ssearch")
    page.wait_for_selector("#srows > li")


def test_the_why_column_opens_with_its_sections_and_esc_restores_the_rows(
    search_url: str, browser: Browser
) -> None:
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(search_url + "/#tab=search")
    page.wait_for_selector("#spoollist li.ptile")
    _search(page)
    thumbs = page.locator("#srows > li:first-child .thumbs button:visible").count()
    assert thumbs > 1
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
    page.keyboard.press("Escape")
    assert page.locator("#swhy").is_hidden()
    assert page.locator("#srows > li:first-child .thumbs button:visible").count() == thumbs
    page.close()


def test_the_page_draws_token_boxes_results_a_lightbox_and_the_partial_pool_banner(
    search_url: str, browser: Browser
) -> None:
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url))
    page.goto(search_url + "/#tab=search")
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
    page.wait_for_selector("#srows > li:first-child .clip")
    page.wait_for_selector("#srows > li:first-child >> text=Stop")
    assert all(r.startswith(search_url) or r.startswith("data:") for r in requests), requests
    page.close()


def test_the_sidebar_grid_counts_each_titles_shots_and_narrows_the_results(
    search_url: str, browser: Browser
) -> None:
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(search_url + "/#tab=search")
    page.wait_for_selector("#spoollist li.ptile")
    assert page.locator("#spoollist li.ptile").count() == 2
    assert page.locator("#spoollist .st").all_inner_texts() == ["", ""]
    _search(page)
    # Heat has two shots and La Haine one.
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
