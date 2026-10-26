"""`admin parity explore` — Plex DB-EX served by Python and by Rust over one snapshot.

Starts the Python explorer and `plexdb-rs explore` on one copy of the store (the Rust one
forwards the routes it has not taken over to the Python one), asks both for every route Rust
answers, and lists the routes whose answers differ. The page, `/api/tags`, `/api/titles`,
`/api/cotags`, `/api/title`, the default `/api/map` and `/api/tagnetwork` and every error
reply must match byte for byte.

A map or network drawn live with tags excluded is laid out by UMAP in Python and by holomap in
Rust, so only the positions differ there: those routes are compared without positions and
region labels (the titles, their count and years, the tags and their title counts, the
edges). Before the comparison the copy's stored maps and networks are redrawn by `plexdb-rs`,
as a sweep's `refresh-map` and `refresh-tagnetwork` redraw them, so the default routes compare
the stored layout rather than two live draws.

    uv run python tools/parity_explore.py [--store data/plexdb.db] [--decisions-dir DIR]

Prints one JSON object: `routes` compared, and `differing` as `[{route, why}]`. Exits 1 when
anything differs.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent

#: A request to the Rust server for a route it answers waits this long: a live draw with
#: noise excluded is the slow one.
TIMEOUT = 900

URL_LINE = re.compile(r"Plex DB-EX on (http://\S+?)/ reading")


def fetch(base: str, route: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(base + route, timeout=TIMEOUT) as reply:
            return reply.status, reply.headers.get("Content-Type", ""), reply.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers.get("Content-Type", ""), err.read()


@contextlib.contextmanager
def serve(argv: list[str], env: dict[str, str]) -> Iterator[str]:
    """Run an explorer and yield its base URL; stop it on the way out."""
    process = subprocess.Popen(
        argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    try:
        assert process.stdout is not None
        line = process.stdout.readline()
        found = URL_LINE.search(line)
        if found is None:
            raise SystemExit(f"error: {argv[:3]} did not report its address, printed {line!r}")
        yield found.group(1)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def structure(route: str, body: bytes) -> object:
    """A live draw's answer without what a layout decides: positions and region labels."""
    if route.startswith("/api/tagnetwork"):
        lines = [json.loads(line) for line in body.decode().splitlines()]
        for line in lines:
            if line.get("stage") == "done":
                line["nodes"] = [[n[0], n[1]] for n in line["nodes"]]
        return lines
    payload = json.loads(body)
    payload["points"] = sorted([p[0], p[1], p[2]] for p in payload["points"])
    payload.pop("labels", None)
    return payload


def compare(route: str, python: tuple[int, str, bytes], rust: tuple[int, str, bytes]) -> str | None:
    if python[0] != rust[0]:
        return f"status {python[0]} in Python, {rust[0]} in Rust"
    if python[1] != rust[1]:
        return f"content type {python[1]!r} in Python, {rust[1]!r} in Rust"
    if "exclude=" in route:
        return None if structure(route, python[2]) == structure(route, rust[2]) else "structure"
    if python[2] == rust[2]:
        return None
    at = next((i for i, (a, b) in enumerate(zip(python[2], rust[2], strict=False)) if a != b), None)
    if at is None:
        at = min(len(python[2]), len(rust[2]))
    window = slice(max(0, at - 40), at + 60)
    return (
        f"bytes differ at {at} ({len(python[2])} in Python, {len(rust[2])} in Rust): "
        f"{python[2][window]!r} against {rust[2][window]!r}"
    )


def routes(python: str, kinds: tuple[str, ...]) -> list[str]:
    """Every route to ask, built from what the Python explorer reports the store holds."""
    asked = ["/", "/api/tags", "/api/tags?kind=bogus", "/api/titles", "/api/cotags"]
    asked += ["/api/cotags?kind=bogus&tag=x", "/api/title", "/api/map?kind=bogus"]
    asked += ["/api/title?item_id=" + "x" * 201, "/api/title?item_id=no%3Asuch"]
    asked += ["/api/tagnetwork?kind=bogus"]
    excludes: list[str] = []
    for kind in kinds:
        asked += [
            f"/api/tags?kind={kind}",
            f"/api/map?kind={kind}",
            f"/api/map?kind={kind}&stored=1",
        ]
        asked += [f"/api/tagnetwork?kind={kind}"]
        _, _, tags_body = fetch(python, f"/api/tags?kind={kind}")
        tags = [t["value"] for t in json.loads(tags_body)["tags"]]
        for tag in [*tags[:3], tags[len(tags) // 2], tags[-1], "no such tag"]:
            quoted = quote(tag)
            asked += [
                f"/api/titles?kind={kind}&tag={quoted}",
                f"/api/cotags?kind={kind}&tag={quoted}",
            ]
        _, _, map_body = fetch(python, f"/api/map?kind={kind}")
        points = json.loads(map_body)["points"]
        for point in [points[0], points[len(points) // 2], points[-1]]:
            asked.append(f"/api/title?item_id={quote(point[0])}")
        if kind == kinds[-1]:
            excludes += [f"/api/map?kind={kind}&exclude={quote(tags[0])}"]
            excludes += [
                f"/api/tagnetwork?kind={kind}&exclude={quote(tags[0])}&exclude={quote(tags[2])}"
            ]
    return asked + excludes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="admin parity explore", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--store", type=Path, default=REPO / "data" / "plexdb.db")
    parser.add_argument("--decisions-dir", type=Path)
    parser.add_argument("--kinds", default="movie,show", help="comma-separated, default movie,show")
    args = parser.parse_args(argv)
    rs_bin = os.environ.get("PLEXDB_RS_BIN", "").strip()
    if not rs_bin:
        print("error: PLEXDB_RS_BIN is not set; `admin parity` builds and sets it", file=sys.stderr)
        return 1
    store = args.store.resolve()
    if not store.is_file():
        print(f"error: no store at {store} — run `admin pull-baseline` first", file=sys.stderr)
        return 1
    decisions = (args.decisions_dir or store.parent).resolve()
    kinds = tuple(args.kinds.split(","))
    differing: list[dict[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="plexdb-parity-explore.") as scratch:
        root = Path(scratch)
        shutil.copyfile(store, root / "plexdb.db")
        for name in sorted(decisions.glob("*.json")):
            shutil.copy(name, root / name.name)
        env = {
            **os.environ,
            "PLEXDB_PATH": str(root / "plexdb.db"),
            "PLEXDB_EXPLORE_SAVED_PATH": str(root / "explore-queries.json"),
        }
        env.pop("PLEXDB_SNAPSHOT_PATH", None)
        for step in ("refresh-map", "refresh-tagnetwork"):
            done = subprocess.run([rs_bin, step], env=env, capture_output=True, text=True)
            if done.returncode != 0:
                print(f"error: {step} failed: {done.stderr.strip()}", file=sys.stderr)
                return 1
        with (
            serve([sys.executable, "-m", "plexdb.cli", "explore", "--port", "0"], env) as python,
            serve([rs_bin, "explore", "--port", "0", "--fallback", python], env) as rust,
        ):
            asked = routes(python, kinds)
            for route in asked:
                why = compare(route, fetch(python, route), fetch(rust, route))
                if why is not None:
                    differing.append({"route": route, "why": why})
    print(json.dumps({"routes": len(asked), "differing": differing}, indent=2))
    return 1 if differing else 0


if __name__ == "__main__":
    raise SystemExit(main())
