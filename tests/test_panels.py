"""The panels file the explorer edits: its shape, its default dashboards, its series keys.

`plexdb record-panels` (`crates/plexdb/src/record_panels.rs`) reads the same files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from plexdb.errors import PanelsFileError
from plexdb.panels import DEFAULT_DASHBOARDS, PanelsFile, series_key

REPO = Path(__file__).resolve().parent.parent


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


def _write_panels(tmp_path: Path, *panels: dict[str, Any]) -> PanelsFile:
    path = tmp_path / "explore-panels.json"
    path.write_text(json.dumps({"dashboards": [{"id": "d", "name": "D", "panels": list(panels)}]}))
    return PanelsFile(path)


def test_the_default_dashboards_are_the_ones_the_rust_sweep_seeds() -> None:
    # `record-panels` seeds a missing panels file from this JSON; the explorer seeds from
    # `DEFAULT_DASHBOARDS`. Until the explorer moves, the two must be one list.
    shipped = json.loads((REPO / "crates/plexdb/src/default_dashboards.json").read_text())
    assert shipped == {"dashboards": json.loads(json.dumps(DEFAULT_DASHBOARDS))}


def test_a_missing_panels_file_is_seeded_with_the_five_library_health_panels(
    tmp_path: Path,
) -> None:
    panels = PanelsFile(tmp_path / "explore-panels.json")

    dashboards = panels.read_or_seed()

    assert [d.name for d in dashboards] == ["Library health"]
    assert [(p.title, p.chart, p.width) for p in dashboards[0].panels] == [
        ("Damaged or rule-breaking files", "line", 2),
        ("English original, no English subtitle", "line", 2),
        ("Decode progress", "gauge", 1),
        ("Tracks per language", "bar", 1),
        ("Extras with more than one file", "table", 2),
    ]
    assert panels.read() == dashboards


def test_a_panels_file_with_no_panels_is_never_reseeded(tmp_path: Path) -> None:
    path = tmp_path / "explore-panels.json"
    path.write_text(json.dumps({"dashboards": [{"id": "d", "name": "D", "panels": []}]}))

    dashboards = PanelsFile(path).read_or_seed()

    assert [(d.id, d.panels) for d in dashboards] == [("d", ())]

    path.write_text(json.dumps({"dashboards": []}))
    assert PanelsFile(path).read_or_seed() == []


@pytest.mark.parametrize(
    ("chart", "settings"),
    [("bar", {"label": "lang"}), ("bar", {"number": "n"}), ("gauge", {})],
)
def test_a_chart_missing_the_settings_it_draws_from_is_refused(
    tmp_path: Path, chart: str, settings: dict[str, str]
) -> None:
    path = tmp_path / "explore-panels.json"
    panel = {**_panel("x", "SELECT 1"), "chart": chart, **settings}
    path.write_text(json.dumps({"dashboards": [{"id": "d", "name": "D", "panels": [panel]}]}))

    with pytest.raises(PanelsFileError, match="needs text"):
        PanelsFile(path).read()


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


def test_a_panel_edit_that_would_pass_the_size_limit_is_refused_and_leaves_the_file(
    tmp_path: Path,
) -> None:
    panels = _write_panels(tmp_path, _panel("a", "SELECT 1"))
    before = panels.path.read_text()

    with pytest.raises(ValueError, match="exceed"):
        panels.upsert("d", {**_panel("b", "SELECT 1"), "id": None, "sql": "x" * (1 << 20)})

    assert panels.path.read_text() == before
