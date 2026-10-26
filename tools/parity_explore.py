"""`admin parity explore` — Plex DB-EX served by Python and by Rust over one snapshot.

Starts the Python explorer and `plexdb-rs explore` on one copy of the store (the Rust one
forwards the routes it has not taken over to the Python one), asks both for every route Rust
answers, and lists the routes whose answers differ. The page, `/api/tags`, `/api/titles`,
`/api/cotags`, `/api/title`, the default `/api/map` and `/api/tagnetwork` and every error
reply must match byte for byte. So must the decision tabs' reads (`/api/merges`, `/api/roles`,
`/api/lists`, `/api/list`) and the tag doctor (`/api/doctor`, `/api/keywords`,
`/api/doctor/titles`), except that the doctor's objects are compared as parsed JSON: Python
builds them from sets, so their key order changes from one run to the next.

The decision writes (`POST /api/merges`, `/api/roles`, `/api/names`, `/api/list-decisions`)
run against a Python explorer and a Rust one that each keep their decisions files in a
directory of their own, seeded alike. The replies must match with the time of each decision
(`decided_at` of today) masked, the reads after the writes must match the same way, and so
must the four decision files at the end.

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
import datetime
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


def fetch(
    base: str,
    route: str,
    body: bytes | None = None,
    content_type: str | None = "application/json",
    method: str | None = None,
) -> tuple[int, str, bytes]:
    request = urllib.request.Request(base + route, data=body, method=method)
    if body is not None and content_type is not None:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
            return reply.status, reply.headers.get("Content-Type", ""), reply.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers.get("Content-Type", ""), err.read()


def today_mask(body: bytes) -> bytes:
    """`body` with the time of every decision made today replaced, so two servers that made
    the same decision a second apart agree."""
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    pattern = rb'"decided_at":\s*"' + today.encode() + rb'T[^"]*"'
    return re.sub(pattern, b'"decided_at":"<now>"', body)


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


#: Routes whose objects Python builds from sets, so only their parsed JSON is comparable.
UNORDERED = ("/api/doctor?",)


def compare(route: str, python: tuple[int, str, bytes], rust: tuple[int, str, bytes]) -> str | None:
    if python[0] != rust[0]:
        return f"status {python[0]} in Python, {rust[0]} in Rust"
    if python[1] != rust[1]:
        return f"content type {python[1]!r} in Python, {rust[1]!r} in Rust"
    if route.startswith(UNORDERED) and python[1] == "application/json":
        same = json.loads(today_mask(python[2])) == json.loads(today_mask(rust[2]))
        return None if same else "parsed JSON differs"
    python = (python[0], python[1], today_mask(python[2]))
    rust = (rust[0], rust[1], today_mask(rust[2]))
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
    return asked + excludes + decision_reads(python, kinds)


_ASKED: dict[str, dict[str, object]] = {}


def get_json(base: str, route: str) -> dict[str, object]:
    """The Python explorer's answer, asked once: `/api/lists` takes minutes on a real store,
    and the routes are built from the answers as they were before any write."""
    if route not in _ASKED:
        _ASKED[route] = json.loads(fetch(base, route)[2])
    return _ASKED[route]


def decision_reads(python: str, kinds: tuple[str, ...]) -> list[str]:
    """The decision tabs' reads and the tag doctor, built from what the store holds."""
    asked = ["/api/merges", "/api/roles", "/api/roles?q=zz", "/api/lists", "/api/list"]
    asked += ["/api/list?ref=" + "x" * 201, "/api/list?ref=no%3Asuch", "/api/lists?source=mdblist"]
    asked += ["/api/doctor", "/api/doctor?item_id=" + "x" * 201, "/api/doctor?item_id=no%3Asuch"]
    asked += ["/api/keywords", "/api/keywords?q=a", "/api/doctor/titles", "/api/doctor/titles?q=a"]
    roles = get_json(python, "/api/roles")
    for entry in roles["keywords"][:3]:  # type: ignore[index]
        asked.append("/api/roles?q=" + quote(entry["form"][:4]))
    lists = get_json(python, "/api/lists")["lists"]
    for row in lists[:4]:  # type: ignore[index]
        asked.append("/api/list?ref=" + quote(row["collection_id"] or row["url"]))
    merges = get_json(python, "/api/merges")
    for pair in [*merges["proposed"][:3], *merges["decided"][:3]]:  # type: ignore[index]
        asked.append("/api/keywords?q=" + quote(pair["form_a"][:5]))
    kind = kinds[0]
    tags = [t["value"] for t in get_json(python, f"/api/tags?kind={kind}")["tags"]]  # type: ignore[union-attr]
    for tag in tags[:3]:
        asked.append("/api/keywords?q=" + quote(tag[:6]))
    _, _, titles_body = fetch(python, f"/api/titles?kind={kind}&tag={quote(tags[0])}")
    for title in json.loads(titles_body)["titles"][:6]:
        asked.append("/api/doctor?item_id=" + quote(title["item_id"]))
        asked.append("/api/doctor/titles?q=" + quote(title["title"][:6]))
    return asked


def writes(python: str) -> list[tuple[str, dict[str, object] | bytes, str | None]]:
    """The decision POSTs to make, in order, as `(route, body, content type)`; a body that is
    bytes is sent as it is."""
    json_type: str | None = "application/json"
    merges = get_json(python, "/api/merges")
    proposed = merges["proposed"]
    decided = merges["decided"]
    steps: list[tuple[str, dict[str, object] | bytes, str | None]] = []
    if proposed:
        pair = proposed[0]  # type: ignore[index]
        base = {"keyword_a": pair["keyword_a"], "keyword_b": pair["keyword_b"]}
        steps += [
            ("/api/merges", {**base, "decision": d}, json_type) for d in ("accepted", "rejected")
        ]
        steps.append(("/api/merges", {**base, "decision": "cleared"}, json_type))
    if decided:
        pair = decided[0]  # type: ignore[index]
        base = {"keyword_a": pair["keyword_a"], "keyword_b": pair["keyword_b"]}
        steps.append(("/api/merges", {**base, "decision": "rejected"}, json_type))
        steps.append(("/api/merges", {**base, "decision": "cleared"}, json_type))
    steps += [
        ("/api/merges", {"keyword_a": "b", "keyword_b": "a", "decision": "accepted"}, json_type),
        ("/api/merges", {"keyword_a": "", "keyword_b": "a", "decision": "accepted"}, json_type),
        ("/api/merges", {"keyword_a": "a", "keyword_b": "b", "decision": "maybe"}, json_type),
        (
            "/api/merges",
            {"keyword_a": "zzzq1", "keyword_b": "zzzq2", "decision": "accepted"},
            json_type,
        ),
        ("/api/merges", {"keyword_a": 1, "keyword_b": "a", "decision": "accepted"}, json_type),
        ("/api/merges", b"[1]", json_type),
        ("/api/merges", b"{}", "text/plain"),
        ("/api/merges", b"{}", None),
    ]
    roles = get_json(python, "/api/roles")["keywords"]
    for entry in roles[:2]:  # type: ignore[index]
        role = next(iter(entry["cells"]))
        base = {"keyword": entry["keyword"], "role": role}
        steps += [
            ("/api/roles", {**base, "decision": d}, json_type) for d in ("accepted", "rejected")
        ]
        steps.append(("/api/roles", {**base, "decision": "cleared"}, json_type))
        steps.append(("/api/roles", {**base, "decision": "accepted"}, json_type))
    steps += [
        ("/api/roles", {"keyword": "zzzq1", "role": "tone", "decision": "accepted"}, json_type),
        ("/api/roles", {"keyword": "a", "role": "mood", "decision": "accepted"}, json_type),
        ("/api/roles", {"keyword": "a", "role": "tone", "decision": "x"}, json_type),
    ]
    tags = [t["value"] for t in get_json(python, "/api/tags?kind=movie")["tags"]]  # type: ignore[union-attr]
    for tag in tags[:2]:
        steps.append(("/api/names", {"keyword": tag, "name": "  Parity   Name  "}, json_type))
    steps += [
        ("/api/names", {"keyword": tags[0], "name": "Second\tName"}, json_type),
        ("/api/names", {"keyword": tags[2], "name": "Héllo \U0001f600"}, json_type),
        ("/api/names", {"keyword": tags[1], "name": None}, json_type),
        ("/api/names", {"keyword": tags[2], "name": "   "}, json_type),
        ("/api/names", {"keyword": tags[3]}, json_type),
        ("/api/names", {"keyword": tags[3], "name": 5}, json_type),
        ("/api/names", {"keyword": tags[3], "name": "x" * 201}, json_type),
        ("/api/names", {"keyword": "zzzq1", "name": "x"}, json_type),
    ]
    lists = get_json(python, "/api/lists")["lists"]
    new = "https://mdblist.com/lists/parity/new-list"
    steps += [
        ("/api/list-decisions", {"url": new, "decision": "added"}, json_type),
        ("/api/list-decisions", {"url": new, "decision": "refetch"}, json_type),
        (
            "/api/list-decisions",
            {"url": "https://mdblist.com/lists/nobody/unknown", "decision": "retry"},
            json_type,
        ),
        ("/api/list-decisions", {"url": "https://example.com/x", "decision": "added"}, json_type),
        ("/api/list-decisions", {"url": new, "decision": "bogus"}, json_type),
        ("/api/list-decisions", {"url": "", "decision": "added"}, json_type),
        (
            "/api/list-decisions",
            {"url": "https://letterboxd.com/Parity/list/Some-List/", "decision": "excluded"},
            json_type,
        ),
    ]
    for row in lists[:2]:  # type: ignore[index]
        if row["url"]:
            steps.append(
                ("/api/list-decisions", {"url": row["url"], "decision": "refetch"}, json_type)
            )
            steps.append(
                ("/api/list-decisions", {"url": row["url"], "decision": "excluded"}, json_type)
            )
    return steps


def send(
    base: str, step: tuple[str, dict[str, object] | bytes, str | None]
) -> tuple[int, str, bytes]:
    route, body, content_type = step
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    return fetch(base, route, data, content_type, "POST")


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
        # Each explorer keeps its decisions files in a directory of its own, so the writes of
        # one never reach the other; both start from the same files. `plexdb explore` keeps
        # them beside the store and ignores PLEXDB_EXPLORE_SAVED_PATH; `plexdb-rs explore`
        # reads it.
        homes = {"python": root, "rust": root / "rust"}
        homes["rust"].mkdir()
        for home in homes.values():
            for name in sorted(decisions.glob("*.json")):
                shutil.copy(name, home / name.name)
        env = {**os.environ, "PLEXDB_PATH": str(root / "plexdb.db")}
        env.pop("PLEXDB_SNAPSHOT_PATH", None)
        rust_env = {**env, "PLEXDB_EXPLORE_SAVED_PATH": str(homes["rust"] / "explore-queries.json")}
        for step in ("refresh-map", "refresh-tagnetwork"):
            done = subprocess.run([rs_bin, step], env=env, capture_output=True, text=True)
            if done.returncode != 0:
                print(f"error: {step} failed: {done.stderr.strip()}", file=sys.stderr)
                return 1
        with (
            serve([sys.executable, "-m", "plexdb.cli", "explore", "--port", "0"], env) as python,
            serve([rs_bin, "explore", "--port", "0", "--fallback", python], rust_env) as rust,
        ):
            asked = routes(python, kinds)
            for route in asked:
                why = compare(route, fetch(python, route), fetch(rust, route))
                if why is not None:
                    differing.append({"route": route, "why": why})
            posted = writes(python)
            for step in posted:
                label = f"POST {step[0]} {step[1]!r}"[:200]
                why = compare("POST", send(python, step), send(rust, step))
                if why is not None:
                    differing.append({"route": label, "why": why})
            after = [
                route
                for route in decision_reads(python, kinds)
                if not route.startswith(("/api/list?", "/api/lists?"))
            ]
            for route in ["/api/merges", "/api/roles", *after]:
                why = compare(route, fetch(python, route), fetch(rust, route))
                if why is not None:
                    differing.append({"route": f"after the writes: {route}", "why": why})
            asked += [f"POST {step[0]}" for step in posted]
            asked += after
        for name in sorted(
            {
                *(p.name for p in homes["python"].glob("*.json")),
                *(p.name for p in homes["rust"].glob("*.json")),
            }
        ):
            files = [
                today_mask((home / name).read_bytes()) if (home / name).exists() else None
                for home in homes.values()
            ]
            asked.append(f"file {name}")
            if files[0] != files[1]:
                differing.append({"route": f"file {name}", "why": "the decisions files differ"})
    print(json.dumps({"routes": len(asked), "differing": differing}, indent=2))
    return 1 if differing else 0


if __name__ == "__main__":
    raise SystemExit(main())
