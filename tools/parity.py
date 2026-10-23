"""`admin parity <step>` — run a sweep step's Python and Rust implementations on two
copies of one store and say which rows they wrote differently.

A step moves to Rust only when this prints `"differing": []` on a fresh `admin
pull-baseline` copy (ADR-0001). It never opens the source store for writing: it
takes two copies with SQLite's backup API, which is safe while a writer has the
source open, and each implementation writes only its own copy.

    uv run python tools/parity.py <step> [--store data/plexdb.db] [--decisions-dir DIR]

Both runs read the same decisions files, from `--decisions-dir` (default: the
source store's directory), through `PLEXDB_EXPLORE_SAVED_PATH`. Each run gets the
`.env` values the Python CLI would load, with `PLEXDB_PATH`, the snapshot and the
backup directory pointed into its own scratch directory.

Prints one JSON object: `tables` and `rows` compared (rows counted on the Python
copy), `differing` as `[{table, key}]`, where `key` is the row's primary key (the
whole row when the table has none, `null` when the table exists on one side
only), and each side's exit code. Exits 1 when anything differs.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

REPO = Path(__file__).resolve().parent.parent


def _python_commands() -> set[str]:
    from plexdb.cli import _command_names, build_parser

    return _command_names(build_parser())


def _rust_steps() -> set[str]:
    from plexdb.rust import steps

    return {step.name for step in steps()}


def _copy(source: Path, target: Path) -> None:
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    # Both implementations open the store in WAL, as the live one is.
    with sqlite3.connect(target) as conn:
        conn.execute("PRAGMA journal_mode = WAL")


def _run(argv: list[str], env: dict[str, str], side: str) -> int:
    done = subprocess.run(argv, env=env, capture_output=True, text=True, check=False, cwd=REPO)
    for line in (done.stdout + done.stderr).splitlines():
        print(f"[{side}] {line}", file=sys.stderr)
    return done.returncode


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
    args = parser.parse_args()

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
    with tempfile.TemporaryDirectory(prefix="plexdb-parity.") as scratch:
        sides = {"python": Path(scratch) / "python", "rust": Path(scratch) / "rust"}
        exits: dict[str, int] = {}
        for side, root in sides.items():
            root.mkdir()
            _copy(store, root / "plexdb.db")
            env = base | {
                "PLEXDB_PATH": str(root / "plexdb.db"),
                "PLEXDB_SNAPSHOT_PATH": str(root / "snapshot.db"),
                "PLEXDB_BACKUP_DIR": str(root / "backups"),
                "PLEXDB_EXPLORE_SAVED_PATH": str(decisions / "explore-queries.json"),
            }
            argv = (
                [sys.executable, "-m", "plexdb.cli", args.step]
                if side == "python"
                else [rs_bin, args.step]
            )
            exits[side] = _run(argv, env, side)
        report = diff(sides["python"] / "plexdb.db", sides["rust"] / "plexdb.db")
    report = {"step": args.step, **report, "exit": exits}
    print(json.dumps(report, indent=2))
    return 0 if not report["differing"] and exits["python"] == exits["rust"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
