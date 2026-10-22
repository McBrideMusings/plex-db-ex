"""Dashboard panels and their nightly values (ADR-0029).

`explore-panels.json` holds the Query tab's dashboards, each a grid of panels, each panel a
saved query with a chart. The sweep runs every panel's SQL once a night through the Query tab's
own guarded `run_query` and keeps the value in `panel-history.json`. Both files sit in
`decisions_dir`, never in `plexdb.db`: a count this project computed is not a fact about a title.

A panel's series is named by `series_key`, which hashes the SQL and the value setting, so an
edit starts a new series and a line never joins two different measurements.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .config import Config
from .decisions import decisions_dir
from .errors import PanelsFileError
from .explore import QueryError, TooManyQueries, run_query

__all__ = [
    "HISTORY_FILE",
    "PANELS_FILE",
    "Dashboard",
    "Panel",
    "PanelHistory",
    "PanelsFile",
    "RecordStats",
    "history_path",
    "panels_path",
    "record_panels",
    "series_key",
]

PANELS_FILE = "explore-panels.json"
HISTORY_FILE = "panel-history.json"

#: A panel's `value` setting: every row counted, or one column of the first row.
ROWS = "rows"
COLUMN_PREFIX = "column:"
HEIGHTS = ("short", "tall")
WIDTHS = range(1, 5)
#: How many hex digits of the SQL-and-value hash a series key keeps.
HASH_DIGITS = 12


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

    OPTIONAL = ("total", "label", "number", "x", "y")

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> list[Dashboard] | None:
        """The dashboards, or None when there is no file. A file that is not valid JSON or
        breaks the shape raises `PanelsFileError`."""
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
        return [self._dashboard(i, d) for i, d in enumerate(raw["dashboards"])]

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
        return Panel(
            id=_text(where, raw, "id"),
            title=_text(where, raw, "title"),
            sql=_text(where, raw, "sql"),
            chart=_text(where, raw, "chart"),
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

    def record(self, results: dict[str, Entry]) -> None:
        """Put each key's entry in its series: the one for that entry's day is replaced, else
        the entry is appended. One atomic write for the lot."""
        with self._lock:
            series = self.read()
            for key, entry in results.items():
                same_day = [e for e in series.get(key, []) if e["day"] == entry["day"]]
                if "error" in entry and any("value" in e for e in same_day):
                    continue  # a rerun that failed never erases the day's recorded number
                entries = [e for e in series.get(key, []) if e["day"] != entry["day"]]
                entries.append(entry)
                series[key] = sorted(entries, key=lambda e: e["day"])
            self._write(series)

    def _write(self, series: dict[str, list[Entry]]) -> None:
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            tmp.write_text(json.dumps({"series": series}, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as err:
            raise PanelsFileError(f"cannot write {self._path}: {err}") from None
        finally:
            tmp.unlink(missing_ok=True)


@dataclass
class RecordStats:
    file_found: bool = False
    recorded: int = 0
    failed: int = 0


def _measure(store_path: Path, panel: Panel) -> float | int:
    """The panel's value now. Raises `QueryError` for anything that is not one number."""
    column = panel.column
    if column is None:
        body = panel.sql.strip().rstrip(";")
        result = run_query(store_path, f"SELECT COUNT(*) FROM (\n{body}\n)")
        return _number(result["rows"][0][0])  # type: ignore[index]
    result = run_query(store_path, panel.sql)
    columns: list[str] = result["columns"]  # type: ignore[assignment]
    rows: list[list[object]] = result["rows"]  # type: ignore[assignment]
    if not rows:
        raise QueryError("the query returned no rows")
    if column not in columns:
        raise QueryError(f"the first row has no column {column}")
    cell = rows[0][columns.index(column)]
    if isinstance(cell, bool) or not isinstance(cell, int | float):
        raise QueryError(f"column {column} is not a number")
    return cell


def _number(cell: object) -> float | int:
    if isinstance(cell, bool) or not isinstance(cell, int | float):
        raise QueryError("the count is not a number")
    return cell


def record_panels(
    store_path: Path,
    panels_file: PanelsFile,
    history: PanelHistory,
    *,
    today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    log: Callable[[str], None] = print,
) -> RecordStats:
    """Record every panel's value for today's UTC date. A panel whose query fails records
    `{day, error}` and the rest still run."""
    stats = RecordStats()
    dashboards = panels_file.read()
    if dashboards is None:
        return stats
    stats.file_found = True
    day = today().isoformat()
    results: dict[str, Entry] = {}
    for dashboard in dashboards:
        for panel in dashboard.panels:
            key = series_key(panel)
            started = time.monotonic()
            try:
                value = _measure(store_path, panel)
                entry: Entry = {"day": day, "value": value}
                shown = f"value {value}"
                stats.recorded += 1
            except (QueryError, TooManyQueries) as err:
                entry = {"day": day, "error": str(err)}
                shown = f"error {err}"
                stats.failed += 1
            elapsed_ms = round((time.monotonic() - started) * 1000)
            results[key] = entry
            log(f"panel {dashboard.id}/{panel.id} {key}: {shown} ({elapsed_ms} ms)")
    if results:
        history.record(results)
    return stats
