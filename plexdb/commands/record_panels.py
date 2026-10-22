"""`plexdb record-panels` — record every dashboard panel's value for tonight."""

from __future__ import annotations

import argparse

from ..config import Config
from ..panels import PanelHistory, PanelsFile, history_path, panels_path, record_panels
from ..sweep import Step

NAME = "record-panels"
#: After `check-files` (22), so a panel counting file verdicts sees tonight's.
ORDER = 23
#: A panel's history is a convenience for a chart, not something a library is published from.
SWEEP = Step.BEST_EFFORT


def _cmd_record_panels(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    path = panels_path(config)
    stats = record_panels(config.store_path, PanelsFile(path), PanelHistory(history_path(config)))
    print(f"panels: {stats.recorded} recorded, {stats.failed} failed, in {history_path(config)}")
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help="run each dashboard panel's SQL (explore-panels.json) and record its value for "
        "today's UTC date in panel-history.json",
    )
    parser.set_defaults(func=_cmd_record_panels)
