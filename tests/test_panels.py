"""`record-panels` records one value per panel per UTC day, in a file beside the store."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from plexdb.errors import PanelsFileError
from plexdb.panels import PanelHistory, PanelsFile, record_panels, series_key
from plexdb.store import init, open_store

DAY1 = date(2026, 10, 1)
DAY2 = date(2026, 10, 2)


def _panel(pid: str, sql: str, value: str = "rows") -> dict[str, Any]:
    return {
        "id": pid,
        "title": pid,
        "sql": sql,
        "chart": "line",
        "value": value,
        "width": 2,
        "height": "short",
    }


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "plexdb.db"
    init(path)
    with open_store(path) as conn:
        conn.executemany(
            "INSERT INTO items (item_id, type, title) VALUES (?, 'movie', ?)",
            [("imdb:tt1", "A"), ("imdb:tt2", "B"), ("imdb:tt3", "C")],
        )
        conn.commit()
    return path


def _write_panels(tmp_path: Path, *panels: dict[str, Any]) -> PanelsFile:
    path = tmp_path / "explore-panels.json"
    path.write_text(json.dumps({"dashboards": [{"id": "d", "name": "D", "panels": list(panels)}]}))
    return PanelsFile(path)


def _record(store: Path, panels: PanelsFile, history: PanelHistory, day: date = DAY1) -> None:
    record_panels(store, panels, history, today=lambda: day, log=lambda _line: None)


def _series_of(history: PanelHistory, panel_id: str) -> list[list[dict[str, Any]]]:
    return [v for k, v in history.read().items() if k.startswith(f"{panel_id}:")]


def test_a_rows_panel_and_a_column_panel_each_record_their_value(
    store: Path, tmp_path: Path
) -> None:
    panels = _write_panels(
        tmp_path,
        _panel("count", "SELECT item_id FROM items"),
        _panel("peak", "SELECT COUNT(*) AS n FROM items WHERE title <> 'C'", "column:n"),
    )
    history = PanelHistory(tmp_path / "panel-history.json")

    _record(store, panels, history)

    assert _series_of(history, "count") == [[{"day": "2026-10-01", "value": 3}]]
    assert _series_of(history, "peak") == [[{"day": "2026-10-01", "value": 2}]]


def test_a_second_run_on_the_same_day_replaces_that_days_entry(store: Path, tmp_path: Path) -> None:
    panels = _write_panels(tmp_path, _panel("count", "SELECT item_id FROM items"))
    history = PanelHistory(tmp_path / "panel-history.json")
    _record(store, panels, history)
    with open_store(store) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('imdb:tt4', 'movie', 'D')")
        conn.commit()

    _record(store, panels, history)
    assert _series_of(history, "count") == [[{"day": "2026-10-01", "value": 4}]]

    _record(store, panels, history, DAY2)
    assert [e["day"] for e in _series_of(history, "count")[0]] == ["2026-10-01", "2026-10-02"]


def test_a_failed_rerun_keeps_the_days_recorded_value(store: Path, tmp_path: Path) -> None:
    history = PanelHistory(tmp_path / "panel-history.json")
    _record(store, _write_panels(tmp_path, _panel("p", "SELECT item_id FROM items")), history)
    key = next(iter(history.read()))

    history.record({key: {"day": "2026-10-01", "error": "query stopped after 5 seconds"}})

    assert history.read()[key] == [{"day": "2026-10-01", "value": 3}]


def test_editing_a_panels_sql_starts_a_second_series(store: Path, tmp_path: Path) -> None:
    history = PanelHistory(tmp_path / "panel-history.json")
    _record(store, _write_panels(tmp_path, _panel("p", "SELECT item_id FROM items")), history)
    edited = _write_panels(tmp_path, _panel("p", "SELECT item_id FROM items WHERE title = 'A'"))

    _record(store, edited, history)

    assert sorted(e[0]["value"] for e in _series_of(history, "p")) == [1, 3]
    assert all(len(e) == 1 for e in _series_of(history, "p"))


def test_a_failing_panel_records_an_error_and_the_others_still_record(
    store: Path, tmp_path: Path
) -> None:
    panels = _write_panels(
        tmp_path,
        _panel("bad", "SELECT nope FROM items"),
        _panel("none", "SELECT n FROM (SELECT 1 AS n) WHERE n = 2", "column:n"),
        _panel("nocol", "SELECT 1 AS n", "column:m"),
        _panel("text", "SELECT title AS t FROM items ORDER BY title", "column:t"),
        _panel("good", "SELECT item_id FROM items"),
    )
    history = PanelHistory(tmp_path / "panel-history.json")

    _record(store, panels, history)

    assert "no such column" in _series_of(history, "bad")[0][0]["error"]
    assert "error" in _series_of(history, "none")[0][0]
    assert "error" in _series_of(history, "nocol")[0][0]
    assert _series_of(history, "text")[0][0]["error"] == "column t is not a number"
    assert _series_of(history, "good") == [[{"day": "2026-10-01", "value": 3}]]


def test_a_missing_panels_file_records_nothing(store: Path, tmp_path: Path) -> None:
    history = PanelHistory(tmp_path / "panel-history.json")

    stats = record_panels(store, PanelsFile(tmp_path / "explore-panels.json"), history)

    assert not stats.file_found
    assert not history.path.exists()


def test_a_malformed_panels_file_is_refused_whole(tmp_path: Path) -> None:
    path = tmp_path / "explore-panels.json"
    path.write_text(json.dumps({"dashboards": [{"id": "d", "name": "D", "panels": [{"id": "x"}]}]}))

    with pytest.raises(PanelsFileError):
        PanelsFile(path).read()


def test_series_key_names_the_series_the_explorer_reads(tmp_path: Path) -> None:
    # The dashboard (plex-db-ex-80d.5) finds a panel's series by this key: a contract.
    panels = _write_panels(tmp_path, _panel("a", "SELECT 1"))
    dashboards = panels.read()
    assert dashboards is not None
    assert series_key(dashboards[0].panels[0]) == "a:2ae978d15fa2"
