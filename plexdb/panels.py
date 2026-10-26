"""Dashboard panels and their nightly values (ADR-0029).

`explore-panels.json` holds the Query tab's dashboards, each a grid of panels, each panel a
saved query with a chart. The sweep runs every panel's SQL once a night through the Query tab's
own guarded `run_query` and keeps the value in `panel-history.json`. The sweep step that
records it is `record-panels` in `crates/plexdb/src/record_panels.rs`; Plex DB-EX reads and edits
the files here. Both files sit in `decisions_dir`, never in `plexdb.db`: a count this project
computed is not a fact about a title.

A panel's series is named by `series_key`, which hashes the SQL and the value setting, so an
edit starts a new series and a line never joins two different measurements.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .decisions import decisions_dir
from .errors import NoSuchPanel, PanelsFileError
from .explore import QueryError, TooManyQueries, run_query
from .media_check import DECODE_TIMED_OUT

__all__ = [
    "DEFAULT_DASHBOARDS",
    "HISTORY_FILE",
    "PANELS_FILE",
    "Dashboard",
    "Panel",
    "PanelHistory",
    "PanelsFile",
    "dashboards_json",
    "history_path",
    "panels_path",
    "series_key",
]

PANELS_FILE = "explore-panels.json"
HISTORY_FILE = "panel-history.json"

#: A panel's `value` setting: every row counted, or one column of the first row.
ROWS = "rows"
COLUMN_PREFIX = "column:"
#: The display settings a chart cannot draw without.
CHART_SETTINGS = {
    "gauge": ("total",),
    "bar": ("label", "number"),
    "pie": ("label", "number"),
    "heatmap": ("x", "y", "number"),
}
CHARTS = ("line", "stat", "gauge", "bar", "pie", "table", "heatmap")
#: `PanelsFile.upsert` and `move` refuse a file that would pass this size, the bound
#: `SavedQueries` puts on its file: Plex DB-EX has no login.
MAX_TOTAL_BYTES = 1 << 20
HEIGHTS = ("short", "tall")
#: The longest dashboard name `PanelsFile.put_dashboard` keeps.
MAX_NAME = 80
WIDTHS = range(1, 5)
#: How many hex digits of the SQL-and-value hash a series key keeps.
HASH_DIGITS = 12


_PANEL_FIELDS = ("id", "title", "sql", "chart", "value", "width", "height")

#: What `explore-panels.json` holds when the file is first created: the Library health
#: dashboard. An existing file, even one with no dashboards, is never seeded again.
DEFAULT_DASHBOARDS: tuple[dict[str, Any], ...] = (
    {
        "id": "library-health",
        "name": "Library health",
        "panels": [
            {
                "id": "damaged-files",
                "title": "Damaged or rule-breaking files",
                "sql": (
                    "SELECT i.title, p.file, c.kind, c.error, c.checked_at\n"
                    "FROM media_check c\n"
                    "JOIN media_part p ON p.part_id = c.part_id\n"
                    "JOIN items i ON i.item_id = p.item_id\n"
                    "WHERE c.ok = 0\n"
                    f"  AND NOT (c.kind = 'decode' AND c.error = '{DECODE_TIMED_OUT}')\n"
                    "ORDER BY c.checked_at DESC, i.title, p.file"
                ),
                "chart": "line",
                "value": "rows",
                "unit": "files",
                "width": 2,
                "height": "short",
            },
            {
                "id": "english-no-english-subtitle",
                "title": "English original, no English subtitle",
                "sql": (
                    "SELECT i.item_id, i.type, i.title, l.value AS original_language\n"
                    "FROM items i\n"
                    "JOIN enrichment l ON l.item_id = COALESCE(i.show_item_id, i.item_id)\n"
                    "                 AND l.namespace = 'tmdb' AND l.key = 'original_language'\n"
                    "WHERE i.type IN ('movie', 'episode')\n"
                    "  AND l.value = 'en'\n"
                    "  AND EXISTS (SELECT 1 FROM media_part p WHERE p.item_id = i.item_id)\n"
                    "  AND NOT EXISTS (\n"
                    "    SELECT 1 FROM media_part p\n"
                    "    JOIN media_stream s ON s.part_id = p.part_id\n"
                    "    WHERE p.item_id = i.item_id AND s.kind = 'subtitle' AND s.lang = 'eng')\n"
                    "ORDER BY i.title, i.item_id"
                ),
                "chart": "line",
                "value": "rows",
                "unit": "titles",
                "width": 2,
                "height": "short",
            },
            {
                "id": "decode-progress",
                "title": "Decode progress",
                "sql": (
                    "SELECT COUNT(c.part_id) AS parts_decoded, COUNT(*) AS parts_total,\n"
                    "       MIN(c.checked_at) AS oldest_decode\n"
                    "FROM media_part p\n"
                    "LEFT JOIN media_check c ON c.part_id = p.part_id AND c.kind = 'decode'"
                ),
                "chart": "gauge",
                "value": "column:parts_decoded",
                "total": "parts_total",
                "unit": "parts decoded in full",
                "width": 1,
                "height": "short",
            },
            {
                "id": "tracks-per-language",
                "title": "Tracks per language",
                "sql": (
                    "SELECT COALESCE(s.lang, '(untagged)') AS lang, COUNT(*) AS tracks\n"
                    "FROM media_stream s\n"
                    "WHERE s.kind IN ('audio', 'subtitle')\n"
                    "GROUP BY lang\n"
                    "ORDER BY tracks DESC"
                ),
                "chart": "bar",
                "value": "rows",
                "label": "lang",
                "number": "tracks",
                "unit": "audio and subtitle tracks",
                "width": 3,
                "height": "tall",
            },
        ],
    },
)


#: Serializes seeding and every edit across the explorer's request threads, which each build
#: their own `PanelsFile`; the sweep, a separate process, relies on the rename being atomic.
_FILE_LOCK = threading.Lock()


def panels_path(config: Config) -> Path:
    return decisions_dir(config) / PANELS_FILE


def history_path(config: Config) -> Path:
    return decisions_dir(config) / HISTORY_FILE


@dataclass(frozen=True)
class Panel:
    id: str
    title: str
    sql: str
    chart: str
    #: `rows` or `column:<name>`.
    value: str
    width: int
    height: str
    #: Display settings the dashboard reads; the sweep never does.
    options: dict[str, Any]

    @property
    def column(self) -> str | None:
        """The column `value` names, or None when it counts rows."""
        return self.value[len(COLUMN_PREFIX) :] if self.value.startswith(COLUMN_PREFIX) else None


@dataclass(frozen=True)
class Dashboard:
    id: str
    name: str
    panels: tuple[Panel, ...]


def series_key(panel: Panel) -> str:
    """`<panel id>:<first 12 hex of sha256(sql NUL value)>`, the name of the panel's series."""
    digest = hashlib.sha256(f"{panel.sql}\0{panel.value}".encode()).hexdigest()
    return f"{panel.id}:{digest[:HASH_DIGITS]}"


def _text(where: str, entry: dict[str, Any], field: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value:
        raise PanelsFileError(f"{where} needs text {field}")
    return value


class PanelsFile:
    """Reads `explore-panels.json`: `{dashboards: [{id, name, panels: [Panel]}]}`."""

    OPTIONAL = ("total", "label", "number", "unit", "x", "y")

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def read_or_seed(self) -> list[Dashboard]:
        """The dashboards; a missing file is first written with `DEFAULT_DASHBOARDS`. A file
        that exists is read as it is, so one with no dashboards stays empty."""
        with _FILE_LOCK:
            dashboards = self.read()
            if dashboards is not None:
                return dashboards
            self._write({"dashboards": list(DEFAULT_DASHBOARDS)})
            dashboards = self.read()
        assert dashboards is not None
        return dashboards

    def _write(self, raw: dict[str, Any]) -> None:
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as err:
            raise PanelsFileError(f"cannot write {self._path}: {err}") from None
        finally:
            tmp.unlink(missing_ok=True)

    def read(self) -> list[Dashboard] | None:
        """The dashboards, or None when there is no file. A file that is not valid JSON or
        breaks the shape raises `PanelsFileError`."""
        raw = self._load()
        if raw is None:
            return None
        return [self._dashboard(i, d) for i, d in enumerate(raw["dashboards"])]

    def _load(self) -> dict[str, Any] | None:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as err:
            raise PanelsFileError(f"cannot read {self._path}: {err}") from None
        try:
            raw = json.loads(text)
        except ValueError as err:
            raise PanelsFileError(f"{self._path} is not valid JSON: {err}") from None
        if not isinstance(raw, dict) or not isinstance(raw.get("dashboards"), list):
            raise PanelsFileError(f"{self._path} does not hold a dashboards list")
        return raw

    def _edit(self) -> dict[str, Any]:
        """The raw file for an edit, seeded first when it is missing. Hold `_FILE_LOCK`."""
        raw = self._load()
        if raw is None:
            return {"dashboards": json.loads(json.dumps(DEFAULT_DASHBOARDS))}
        for i, d in enumerate(raw["dashboards"]):
            self._dashboard(i, d)  # the whole file must hold the shape before an edit rewrites it
        return raw

    def _save(self, raw: dict[str, Any]) -> None:
        if len((json.dumps(raw, indent=2) + "\n").encode()) > MAX_TOTAL_BYTES:
            raise ValueError(f"the panels would exceed {MAX_TOTAL_BYTES} bytes")
        self._write(raw)

    @staticmethod
    def _find(raw: dict[str, Any], dashboard_id: str) -> list[dict[str, Any]]:
        for d in raw["dashboards"]:
            if d["id"] == dashboard_id:
                return d["panels"]  # type: ignore[no-any-return]
        raise NoSuchPanel(f"dashboard {dashboard_id}")

    def upsert(self, dashboard_id: object, panel: object) -> dict[str, Any]:
        """Create `panel` in the dashboard (no `id`: a new uuid, appended) or replace the one
        with its `id` in place. Returns the stored panel."""
        if not isinstance(dashboard_id, str) or not isinstance(panel, dict):
            raise ValueError("dashboard_id must be text and panel an object")
        entry = {k: panel[k] for k in (*_PANEL_FIELDS, *self.OPTIONAL) if k in panel}
        given = entry.get("id")
        entry["id"] = uuid.uuid4().hex if given is None else given
        if not isinstance(entry["id"], str) or ":" in entry["id"]:
            raise ValueError("id must be text without a colon")
        try:
            self._panel("panel", entry)
        except PanelsFileError as err:
            raise ValueError(str(err)) from None
        for k in self.OPTIONAL:
            if k in entry and (not isinstance(entry[k], str) or not entry[k]):
                raise ValueError(f"{k} must be text")
        with _FILE_LOCK:
            raw = self._edit()
            panels = self._find(raw, dashboard_id)
            at = next((i for i, p in enumerate(panels) if p["id"] == entry["id"]), None)
            if at is not None:
                panels[at] = entry
            elif given is not None:
                raise NoSuchPanel(f"panel {given}")
            else:
                panels.append(entry)
            self._save(raw)
        return entry

    def move(self, dashboard_id: object, panel_id: object, to: object) -> list[str]:
        """Put the panel at index `to` of its dashboard; returns the dashboard's panel ids."""
        if not isinstance(dashboard_id, str) or not isinstance(panel_id, str):
            raise ValueError("dashboard_id and panel_id must be text")
        if isinstance(to, bool) or not isinstance(to, int):
            raise ValueError("to must be a whole number")
        with _FILE_LOCK:
            raw = self._edit()
            panels = self._find(raw, dashboard_id)
            at = next((i for i, p in enumerate(panels) if p["id"] == panel_id), None)
            if at is None:
                raise NoSuchPanel(f"panel {panel_id}")
            if not 0 <= to < len(panels):
                raise ValueError(f"to must be 0 to {len(panels) - 1}")
            panels.insert(to, panels.pop(at))
            self._save(raw)
            return [p["id"] for p in panels]

    def put_dashboard(self, dashboard_id: object, name: object) -> Dashboard:
        """Create a dashboard (no `dashboard_id`: a new uuid, appended, with no panels) or rename
        the one with that id. Returns it."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be text")
        name = name.strip()
        if len(name) > MAX_NAME:
            raise ValueError(f"name is over {MAX_NAME} characters")
        if dashboard_id is not None and (
            not isinstance(dashboard_id, str) or not dashboard_id or ":" in dashboard_id
        ):
            raise ValueError("id must be text without a colon")
        with _FILE_LOCK:
            raw = self._edit()
            if dashboard_id is None:
                entry: dict[str, Any] = {"id": uuid.uuid4().hex, "name": name, "panels": []}
                raw["dashboards"].append(entry)
            else:
                entry = next((d for d in raw["dashboards"] if d["id"] == dashboard_id), {})
                if not entry:
                    raise NoSuchPanel(f"dashboard {dashboard_id}")
                entry["name"] = name
            self._save(raw)
        return self._dashboard(0, entry)

    def delete_dashboard(self, dashboard_id: str) -> list[str]:
        """Remove the dashboard; returns the ids of the panels that went with it. The last
        dashboard is refused."""
        with _FILE_LOCK:
            raw = self._edit()
            at = next((i for i, d in enumerate(raw["dashboards"]) if d["id"] == dashboard_id), None)
            if at is None:
                raise NoSuchPanel(f"dashboard {dashboard_id}")
            if len(raw["dashboards"]) == 1:
                raise ValueError("the last dashboard cannot be deleted")
            gone = raw["dashboards"].pop(at)
            self._save(raw)
        return [p["id"] for p in gone["panels"]]

    def delete(self, panel_id: str) -> None:
        """Remove the panel from whichever dashboard holds it."""
        with _FILE_LOCK:
            raw = self._edit()
            for d in raw["dashboards"]:
                kept = [p for p in d["panels"] if p["id"] != panel_id]
                if len(kept) != len(d["panels"]):
                    d["panels"] = kept
                    self._save(raw)
                    return
        raise NoSuchPanel(f"panel {panel_id}")

    def _dashboard(self, index: int, raw: Any) -> Dashboard:
        where = f"{self._path} dashboard {index}"
        if not isinstance(raw, dict) or not isinstance(raw.get("panels"), list):
            raise PanelsFileError(f"{where} needs text id, name and a panels list")
        return Dashboard(
            id=_text(where, raw, "id"),
            name=_text(where, raw, "name"),
            panels=tuple(self._panel(f"{where} panel {i}", p) for i, p in enumerate(raw["panels"])),
        )

    def _panel(self, where: str, raw: Any) -> Panel:
        if not isinstance(raw, dict):
            raise PanelsFileError(f"{where} is not an object")
        value = _text(where, raw, "value")
        if value != ROWS and not (
            value.startswith(COLUMN_PREFIX) and len(value) > len(COLUMN_PREFIX)
        ):
            raise PanelsFileError(f"{where} has value {value!r}, expected rows or column:<name>")
        width = raw.get("width")
        if isinstance(width, bool) or not isinstance(width, int) or width not in WIDTHS:
            raise PanelsFileError(f"{where} has width {width!r}, expected 1 to 4")
        height = raw.get("height")
        if height not in HEIGHTS:
            raise PanelsFileError(f"{where} has height {height!r}, expected one of {HEIGHTS}")
        chart = _text(where, raw, "chart")
        if chart not in CHARTS:
            raise PanelsFileError(f"{where} has chart {chart!r}, expected one of {CHARTS}")
        for needed in CHART_SETTINGS.get(chart, ()):
            _text(where, raw, needed)
        return Panel(
            id=_text(where, raw, "id"),
            title=_text(where, raw, "title"),
            sql=_text(where, raw, "sql"),
            chart=chart,
            value=value,
            width=width,
            height=height,
            options={k: raw[k] for k in self.OPTIONAL if k in raw},
        )


#: One night's entry: `{day, value}` or `{day, error}`.
Entry = dict[str, Any]


class PanelHistory:
    """`panel-history.json`: `{series: {"<panel id>:<hash12>": [{day, value} | {day, error}]}}`.

    Replaced by rename so a reader never sees half of it, the way `SavedQueries` writes its file.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> dict[str, list[Entry]]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as err:
            raise PanelsFileError(f"cannot read {self._path}: {err}") from None
        try:
            raw = json.loads(text)
        except ValueError as err:
            raise PanelsFileError(f"{self._path} is not valid JSON: {err}") from None
        series = raw.get("series") if isinstance(raw, dict) else None
        if not isinstance(series, dict) or not all(
            isinstance(entries, list)
            and all(isinstance(e, dict) and isinstance(e.get("day"), str) for e in entries)
            for entries in series.values()
        ):
            raise PanelsFileError(f"{self._path} does not hold a series map of day entries")
        return {str(k): v for k, v in series.items()}

    def drop_panel(self, panel_id: str) -> int:
        """Remove every series whose key starts with `<panel id>:`; returns how many went."""
        with self._lock:
            series = self.read()
            gone = [k for k in series if k.startswith(f"{panel_id}:")]
            if gone:
                self._write({k: v for k, v in series.items() if k not in gone})
            return len(gone)

    def _write(self, series: dict[str, list[Entry]]) -> None:
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            tmp.write_text(json.dumps({"series": series}, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as err:
            raise PanelsFileError(f"cannot write {self._path}: {err}") from None
        finally:
            tmp.unlink(missing_ok=True)


def dashboards_json(
    store_path: Path, dashboards: list[Dashboard], history: dict[str, list[Entry]]
) -> list[dict[str, Any]]:
    """The `/api/dashboards` body: each panel with the rows its SQL returns now (`live`) and
    its recorded `series`, oldest night first."""
    out: list[dict[str, Any]] = []
    for dashboard in dashboards:
        panels = []
        for panel in dashboard.panels:
            try:
                result = run_query(store_path, panel.sql)
                live: dict[str, Any] = {
                    "columns": result["columns"],
                    "rows": result["rows"],
                    "truncated": result["truncated"],
                }
            except (QueryError, TooManyQueries) as err:
                live = {"error": str(err)}
            panels.append(
                {
                    "id": panel.id,
                    "title": panel.title,
                    "sql": panel.sql,
                    "chart": panel.chart,
                    "value": panel.value,
                    "width": panel.width,
                    "height": panel.height,
                    **panel.options,
                    "live": live,
                    "series": history.get(series_key(panel), []),
                }
            )
        out.append({"id": dashboard.id, "name": dashboard.name, "panels": panels})
    return out
