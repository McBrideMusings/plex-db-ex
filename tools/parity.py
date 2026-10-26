"""`admin parity <step>` — run a sweep step's Python and Rust implementations on two
copies of one store and say which rows they wrote differently.

A step moves to Rust only when this prints `"differing": []` on a fresh `admin
pull-baseline` copy (ADR-0001). It never opens the source store for writing: it
takes two copies with SQLite's backup API, which is safe while a writer has the
source open, and each implementation writes only its own copy.

    uv run python tools/parity.py <step> [--store data/plexdb.db] [--decisions-dir DIR] [step args]

Both runs start from the decisions files in `--decisions-dir` (default: the source
store's directory), each on its own copy, through `PLEXDB_EXPLORE_SAVED_PATH`. Each run
gets the `.env` values the Python CLI would load, with `PLEXDB_PATH`, the snapshot and the
backup directory pointed into its own scratch directory. Arguments the tool does not know
go to the step.

A crowd-list harvest reads two live services, whose answers change between two runs and
whose daily quota a real run spends. Those steps run against `tools/fake_lists.py`, one
local server built from the store being compared and answering both runs the same bytes;
its decision rows are written to both copies first.

Prints one JSON object: `tables` and `rows` compared (rows counted on the Python
copy), `differing` as `[{table, key}]`, where `key` is the row's primary key (the
whole row when the table has none, `null` when the table exists on one side
only), each side's exit code, whether the two printed the same lines (`stdout`), and,
for a step that talks to a server, whether the two sent it the same requests
(`requests`). A time a run wrote is not compared (two runs are seconds apart), only that
it was written. Exits 1 when anything differs.
"""

from __future__ import annotations

import argparse
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

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

#: Steps that read MDBList or Letterboxd: they run against `tools/fake_lists.py`.
SERVER_STEPS = frozenset({"harvest-mdblist", "harvest-letterboxd-lists", "harvest-mdblist-search"})
#: Steps whose printed lines are part of what moved, so the two must print the same ones.
OUTPUT_STEPS = SERVER_STEPS | {"lists", "list", "list-decide"}
#: Decisions files a step may write, compared after it ran.
DECISIONS_FILES = ("list_decisions.json",)

_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?\+00:00")
_TIME_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*+00:00"


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


def _differences(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    """The rows of `table` that one copy holds and the other does not, as their keys."""
    t = _quote(table)
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
    return keys


def _blank_run_times(conn: sqlite3.Connection, table: str, since: str) -> None:
    """Replace every time a run wrote into `table` (an ISO time at or after `since`, which
    only the run could have written) with one value, in both copies. Two runs are seconds
    apart, so their times differ whatever the steps did."""
    t = _quote(table)
    for schema in ("main", "rs"):
        for col in conn.execute(f"PRAGMA {schema}.table_info({t})").fetchall():
            if col[2].upper().startswith("TEXT"):
                c = _quote(col[1])
                conn.execute(
                    f"UPDATE {schema}.{t} SET {c} = '<run time>' WHERE {c} >= ? AND {c} GLOB ?",
                    (since, _TIME_GLOB),
                )
    conn.commit()


def diff(python_store: Path, rust_store: Path, since: str | None = None) -> dict[str, Any]:
    conn = sqlite3.connect(python_store)
    conn.execute("ATTACH DATABASE ? AS rs", (str(rust_store),))
    py_tables, rs_tables = _tables(conn, "main"), _tables(conn, "rs")
    differing: list[dict[str, Any]] = []
    rows = 0
    for table in sorted(py_tables.keys() | rs_tables.keys()):
        if py_tables.get(table) != rs_tables.get(table):
            differing.append({"table": table, "key": None})
            continue
        rows += conn.execute(f"SELECT COUNT(*) FROM main.{_quote(table)}").fetchone()[0]
        keys = _differences(conn, table)
        if keys and since is not None:
            _blank_run_times(conn, table, since)
            keys = _differences(conn, table)
        differing += [{"table": table, "key": _jsonable(key)} for key in keys]
    conn.close()
    return {
        "tables": len(py_tables.keys() | rs_tables.keys()),
        "rows": rows,
        "differing": differing,
    }


def _jsonable(key: dict[str, Any]) -> dict[str, Any]:
    return {k: v.hex() if isinstance(v, bytes) else v for k, v in key.items()}


def _lines(text: str, scratch: str) -> list[str]:
    return [_TIME.sub("<time>", line.replace(scratch, "<run>")) for line in text.splitlines()]


def _compare(left: list[str], right: list[str]) -> dict[str, Any]:
    if left == right:
        return {"identical": True}
    delta = list(difflib.unified_diff(left, right, "python", "rust", lineterm="", n=1))
    return {"identical": False, "diff": delta[:60]}


def main() -> int:
    parser = argparse.ArgumentParser(prog="admin parity", description=__doc__.splitlines()[0])
    parser.add_argument("step")
    parser.add_argument("--store", type=Path, default=REPO / "data" / "plexdb.db")
    parser.add_argument("--decisions-dir", type=Path)
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
    scenario = server = None
    server_env: dict[str, str] = {}
    if args.step in SERVER_STEPS:
        import fake_lists

        scenario = fake_lists.build(store)
        server, url = fake_lists.serve(scenario)
        server_env = {
            "MDBLIST_BASE_URL": url,
            "LETTERBOXD_BASE_URL": url,
            "MDBLIST_API_KEY": fake_lists.API_KEY,
            "LETTERBOXD_PAUSE_SECONDS": "0",
        }
    started = datetime.now(UTC).isoformat(timespec="seconds")
    with tempfile.TemporaryDirectory(prefix="plexdb-parity.") as scratch:
        sides = {"python": Path(scratch) / "python", "rust": Path(scratch) / "rust"}
        exits: dict[str, int] = {}
        printed: dict[str, list[str]] = {}
        asked: dict[str, list[str]] = {}
        written: dict[str, list[str]] = {}
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
            env = (
                base
                | server_env
                | {
                    "PLEXDB_PATH": str(root / "plexdb.db"),
                    "PLEXDB_SNAPSHOT_PATH": str(root / "snapshot.db"),
                    "PLEXDB_BACKUP_DIR": str(root / "backups"),
                    "PLEXDB_EXPLORE_SAVED_PATH": str(own / "explore-queries.json"),
                }
            )
            argv = (
                [sys.executable, "-m", "plexdb.cli", args.step, *step_args]
                if side == "python"
                else [rs_bin, args.step, *step_args]
            )
            exits[side], out = _run(argv, env, side)
            printed[side] = _lines(out, str(root))
            if scenario is not None:
                asked[side] = list(scenario.requests)
            written[side] = [
                _lines((own / name).read_text(encoding="utf-8"), str(root))
                if (own / name).exists()
                else []
                for name in DECISIONS_FILES
            ]
        if server is not None:
            server.shutdown()
        report = diff(sides["python"] / "plexdb.db", sides["rust"] / "plexdb.db", started)
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
