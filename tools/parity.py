"""`admin parity <step>` — run a sweep step's Python and Rust implementations on two
copies of one store and say which rows they wrote differently.

A step moves to Rust only when this prints `"differing": []` on a fresh `admin
pull-baseline` copy (ADR-0001). It never opens the source store for writing: it
takes two copies with SQLite's backup API, which is safe while a writer has the
source open, and each implementation writes only its own copy.

    uv run python tools/parity.py <step> [--store data/plexdb.db] [--decisions-dir DIR]
                                  [--fake tmdb|lists|jev|plex|sources] [step args]

Both runs start from the decisions files in `--decisions-dir` (default: the source
store's directory), each on its own copy, through `PLEXDB_EXPLORE_SAVED_PATH`. Arguments
the tool does not know go to the step. Each run gets the
`.env` values the Python CLI would load, with `PLEXDB_PATH`, the snapshot and the
backup directory pointed into its own scratch directory.

A step that fetches runs against a fake server (`tools/parity_fakes.py` for TMDB,
`tools/fake_lists.py` for the MDBList and Letterboxd lists, `tools/fake_sources.py` for AniList,
Wikidata, MDBList ratings and Letterboxd film pages, `parity_fakes.serve_jev` for the embedding
server and Jev, and `parity_fakes.serve_plex` for Plex's `/accounts`; `--fake`) so both sides see
the same answers offline. A fake built from the store also seeds both copies with the decisions the
step acts on and logs the requests each side sent. Times a run wrote (`fetched_at`,
`last_seen` and the other `*_at` and `*_seen` columns at or after the moment parity
started) differ by seconds between two runs by nature, so both copies have them
replaced by one marker before they are compared; a time written earlier is compared
as it is.

Prints one JSON object: `tables` and `rows` compared (rows counted on the Python
copy), `differing` as `[{table, key}]`, where `key` is the row's primary key (the
whole row when the table has none, `null` when the table exists on one side
only), each side's exit code, whether the two printed the same lines (`stdout`), whether
they left the same decisions files, and, against a request-logging fake, whether they sent
the same requests (`requests`). Exits 1 when anything differs. The printed lines must match
for the steps in `OUTPUT_STEPS`.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_lists  # noqa: E402
import fake_sources  # noqa: E402
import parity_fakes  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

#: Commands whose run writes `frames.db`: parity gives each side its own Frame index.
FRAME_COMMANDS = {"frames", "harvest-frames"}

#: Steps whose printed lines are part of what moved, so the two must print the same ones.
OUTPUT_STEPS = frozenset(
    {
        "frames",
        "harvest-frames",
        "harvest-mdblist",
        "harvest-letterboxd-lists",
        "harvest-mdblist-search",
        "lists",
        "list",
        "list-decide",
        "prune-keyword-verdicts",
        "film-suffix-verdicts",
        "judge-keyword-pairs",
        "judge-keyword-roles",
        "latent-users",
        "record-panels",
        "enrich-anilist",
        "enrich-wikidata",
        "enrich-mdblist-ratings",
        "enrich-letterboxd",
    }
)
#: Decisions files a step may write, compared after it ran.
DECISIONS_FILES = ("list_decisions.json", "explore-panels.json", "panel-history.json")

_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?\+00:00")
#: How long a harvest stage took, which differs by milliseconds between two runs.
_STAGE_SECONDS = re.compile(r"\b((?:decode|scan|extract|encode)_s)=\d+\.\d\b")
#: The seconds a client reports a request took, which differ between two runs by nature.
_TOOK = re.compile(r"\b(in|after) \d+\.\d s\b")
#: The milliseconds a panel's query took.
_MS = re.compile(r"\(\d+ ms\)")


def _python_commands() -> set[str]:
    from plexdb.cli import _command_names, build_parser

    return _command_names(build_parser())


def _rust_steps() -> set[str]:
    from plexdb.rust import names

    return names()


def _copy(source: Path, target: Path) -> None:
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    # Both implementations open the store in WAL, as the live one is.
    with sqlite3.connect(target) as conn:
        conn.execute("PRAGMA journal_mode = WAL")


def _run(argv: list[str], env: dict[str, str], side: str) -> tuple[int, str]:
    done = subprocess.run(argv, env=env, capture_output=True, text=True, check=False, cwd=REPO)
    for line in (done.stdout + done.stderr).splitlines():
        print(f"[{side}] {line}", file=sys.stderr)
    return done.returncode, done.stdout


def _lines(text: str, scratch: str) -> list[str]:
    return [
        _MS.sub(
            "(<t> ms)",
            _STAGE_SECONDS.sub(
                r"\1=<s>",
                _TOOK.sub(r"\1 <t> s", _TIME.sub("<time>", line.replace(scratch, "<run>"))),
            ),
        )
        for line in text.splitlines()
    ]


def _compare(left: list[str], right: list[str]) -> dict[str, Any]:
    if left == right:
        return {"identical": True}
    delta = list(difflib.unified_diff(left, right, "python", "rust", lineterm="", n=1))
    return {"identical": False, "diff": delta[:60]}


RUN_TIME = "<written by this run>"


def _mask_run_times(store: Path, started: str) -> None:
    """Replace every `*_at` and `*_seen` text value at or after `started` with one marker."""
    with sqlite3.connect(store) as conn:
        for table in _tables(conn, "main"):
            t = _quote(table)
            for _, column, kind, *_ in conn.execute(f"PRAGMA table_info({t})").fetchall():
                if column.endswith(("_at", "_seen")) and kind.upper() == "TEXT":
                    c = _quote(column)
                    conn.execute(f"UPDATE {t} SET {c} = ? WHERE {c} >= ?", (RUN_TIME, started))


def _tables(conn: sqlite3.Connection, schema: str) -> dict[str, str]:
    """Every ordinary table in `schema`, with its `CREATE` text. A virtual table is
    left out: its rows are in its shadow tables, which are listed."""
    rows = conn.execute(
        f"SELECT name, sql FROM {schema}.sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' AND sql NOT LIKE 'CREATE VIRTUAL TABLE%'"
    )
    return {name: sql for name, sql in rows}


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def diff(python_store: Path, rust_store: Path) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{python_store}?mode=ro", uri=True)
    conn.execute("ATTACH DATABASE ? AS rs", (f"file:{rust_store}?mode=ro",))
    py_tables, rs_tables = _tables(conn, "main"), _tables(conn, "rs")
    differing: list[dict[str, Any]] = []
    rows = 0
    for table in sorted(py_tables.keys() | rs_tables.keys()):
        if py_tables.get(table) != rs_tables.get(table):
            differing.append({"table": table, "key": None})
            continue
        t = _quote(table)
        rows += conn.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
        info = conn.execute(f"PRAGMA main.table_info({t})").fetchall()
        columns = [col[1] for col in info]
        pk = [col[1] for col in sorted(info, key=lambda col: col[5]) if col[5] > 0]
        key_cols = pk or columns
        keys: list[dict[str, Any]] = []
        for first, second in (("main", "rs"), ("rs", "main")):
            query = f"SELECT * FROM {first}.{t} EXCEPT SELECT * FROM {second}.{t}"
            for row in conn.execute(query):
                record = dict(zip(columns, row, strict=True))
                key = {col: record[col] for col in key_cols}
                if key not in keys:
                    keys.append(key)
        differing += [{"table": table, "key": _jsonable(key)} for key in keys]
    conn.close()
    return {
        "tables": len(py_tables.keys() | rs_tables.keys()),
        "rows": rows,
        "differing": differing,
    }


def _jsonable(key: dict[str, Any]) -> dict[str, Any]:
    return {k: v.hex() if isinstance(v, bytes) else v for k, v in key.items()}


def main() -> int:
    parser = argparse.ArgumentParser(prog="admin parity", description=__doc__.splitlines()[0])
    parser.add_argument("step")
    parser.add_argument("--store", type=Path, default=REPO / "data" / "plexdb.db")
    parser.add_argument("--decisions-dir", type=Path)
    parser.add_argument(
        "--frames-db", type=Path, help="seed both sides' frames.db from this file (frame commands)"
    )
    parser.add_argument(
        "--fake",
        choices=["tmdb", "lists", "jev", "plex", "sources", "frames"],
        help="answer the step's network calls from a local fake instead of the real service",
    )
    args, step_args = parser.parse_known_args()

    rs_bin = os.environ.get("PLEXDB_RS_BIN", "").strip()
    if not rs_bin:
        print("error: PLEXDB_RS_BIN is not set; `admin parity` builds and sets it", file=sys.stderr)
        return 1
    if args.step not in _python_commands():
        print(f"error: Python has no {args.step} left to compare", file=sys.stderr)
        return 1
    if args.step not in _rust_steps():
        print(f"error: plexdb/rust_steps.json declares no step {args.step}", file=sys.stderr)
        return 1
    store = args.store.resolve()
    if not store.is_file():
        print(f"error: no store at {store} — run `admin pull-baseline` first", file=sys.stderr)
        return 1
    decisions = (args.decisions_dir or store.parent).resolve()

    base = {k: v for k, v in dotenv_values(REPO / ".env").items() if v is not None}
    base.update(os.environ)
    base.pop("PLEXDB_HELD_STORE", None)
    started = datetime.now(UTC).isoformat(timespec="seconds")
    with contextlib.ExitStack() as stack:
        scratch = stack.enter_context(tempfile.TemporaryDirectory(prefix="plexdb-parity."))
        scenario = None
        if args.fake == "tmdb":
            base.update(stack.enter_context(parity_fakes.serve_tmdb(store)))
        elif args.fake == "plex":
            base.update(stack.enter_context(parity_fakes.serve_plex(store)))
        elif args.fake == "lists":
            served, scenario = stack.enter_context(fake_lists.serve_lists(store))
            base.update(served)
        elif args.fake == "jev":
            served, scenario = stack.enter_context(parity_fakes.serve_jev(store))
            base.update(served)
        elif args.fake == "frames":
            served, scenario = stack.enter_context(parity_fakes.serve_frames(store))
        elif args.fake == "sources":
            served, scenario = stack.enter_context(fake_sources.serve_sources(store))
            base.update(served)
        sides = {"python": Path(scratch) / "python", "rust": Path(scratch) / "rust"}
        exits: dict[str, int] = {}
        printed: dict[str, list[str]] = {}
        asked: dict[str, list[str]] = {}
        written: dict[str, list[list[str]]] = {}
        for side, root in sides.items():
            root.mkdir()
            _copy(store, root / "plexdb.db")
            own = root / "decisions"
            own.mkdir()
            for name in sorted(decisions.glob("*.json")):
                shutil.copy(name, own / name.name)
            if scenario is not None:
                with sqlite3.connect(root / "plexdb.db") as seeded:
                    scenario.seed(seeded)
                scenario.requests.clear()
            env = base | {
                "PLEXDB_PATH": str(root / "plexdb.db"),
                "PLEXDB_SNAPSHOT_PATH": str(root / "snapshot.db"),
                "PLEXDB_BACKUP_DIR": str(root / "backups"),
                "PLEXDB_EXPLORE_SAVED_PATH": str(own / "explore-queries.json"),
            }
            if args.step in FRAME_COMMANDS:
                frames_dir = root / "frames"
                frames_dir.mkdir()
                if args.frames_db is not None:
                    shutil.copyfile(args.frames_db, frames_dir / "frames.db")
                if isinstance(scenario, parity_fakes.FramesScenario):
                    scenario.seed_frames(frames_dir / "frames.db")
                    scenario.requests.clear()
                # Run times compare as text, so both sides must write the same UTC offset.
                env = env | {"PLEXDB_FRAMES_DIR": str(frames_dir), "TZ": "UTC"}
            argv = (
                [sys.executable, "-m", "plexdb.cli", args.step, *step_args]
                if side == "python"
                else [rs_bin, args.step, *step_args]
            )
            exits[side], out = _run(argv, env, side)
            printed[side] = _lines(out, str(root))
            if scenario is not None:
                asked[side] = (
                    sorted(scenario.requests) if args.fake == "jev" else list(scenario.requests)
                )
            written[side] = [
                _lines((own / name).read_text(encoding="utf-8"), str(root))
                if (own / name).exists()
                else []
                for name in DECISIONS_FILES
            ]
            _mask_run_times(root / "plexdb.db", started)
            if args.step in FRAME_COMMANDS and (root / "frames" / "frames.db").exists():
                _mask_run_times(root / "frames" / "frames.db", started)
        report = diff(sides["python"] / "plexdb.db", sides["rust"] / "plexdb.db")
        if args.step in FRAME_COMMANDS:
            frames_report = diff(
                sides["python"] / "frames" / "frames.db", sides["rust"] / "frames" / "frames.db"
            )
            floats = [side / "frames" / "frames-float.db" for side in sides.values()]
            if any(path.exists() for path in floats):
                # `--keep-float`: the measurement file is part of what the run wrote.
                for path in floats:
                    if not path.exists():
                        sqlite3.connect(path).close()
                float_report = diff(floats[0], floats[1])
                frames_report = {
                    "tables": frames_report["tables"] + float_report["tables"],
                    "rows": frames_report["rows"] + float_report["rows"],
                    "differing": frames_report["differing"]
                    + [{**d, "table": "float:" + d["table"]} for d in float_report["differing"]],
                }
            report = {
                "tables": report["tables"] + frames_report["tables"],
                "rows": report["rows"] + frames_report["rows"],
                "differing": report["differing"]
                + [{**d, "db": "frames.db"} for d in frames_report["differing"]],
            }
    report = {"step": args.step, **report, "exit": exits}
    report["stdout"] = _compare(printed["python"], printed["rust"])
    report["decisions_files"] = {
        name: _compare(written["python"][i], written["rust"][i])
        for i, name in enumerate(DECISIONS_FILES)
    }
    if scenario is not None:
        report["requests"] = {
            "python": len(asked["python"]),
            "rust": len(asked["rust"]),
            **_compare(asked["python"], asked["rust"]),
        }
    print(json.dumps(report, indent=2))
    same = (
        not report["differing"]
        and exits["python"] == exits["rust"]
        and all(c["identical"] for c in report["decisions_files"].values())
        and (args.step not in OUTPUT_STEPS or report["stdout"]["identical"])
        and report.get("requests", {"identical": True})["identical"]
    )
    return 0 if same else 1


if __name__ == "__main__":
    raise SystemExit(main())
