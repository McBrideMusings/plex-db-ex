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

The Query tab (`/api/query`, `/api/recipes`, `/api/saved`, `/api/dashboards`, `/api/panels`)
runs the same way: reads first, then an ordered set of saves, edits, moves and deletes against
each server's own files, then the reads again and the files. A new dashboard or panel gets a
random id, so each later step names the id its own server handed back, and ids and a
statement's `elapsed_ms` are masked before the comparison.

The Search tab (`/api/search/*`) runs over a fixture: two of the store's movies get a seeded
`frames.db` (one copy per server, since `queue` writes to it), an encoder on a local port that
embeds every phrase as the same unit vector (and refuses the phrase `refuse me`), a Plex on a
local port that names a three-second `testsrc` video as the first title's part 11, and a
WordPiece tokenizer. Every route and refusal is asked of both servers; a frame and a clip are
compared byte for byte with their `Range` variants and the headers `_send_file` sets; `why` is
compared as parsed JSON with its numbers within 1e-5 (numpy's float32 BLAS and Rust sum the
phrase similarity in different orders) and its order kept strict.

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
    """`body` with what two servers cannot agree on replaced: the time of every decision made
    today, so two servers that made the same decision a second apart agree; a statement's
    `elapsed_ms`; and a new dashboard's or panel's random id."""
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    pattern = rb'"decided_at":\s*"' + today.encode() + rb'T[^"]*"'
    body = re.sub(pattern, b'"decided_at":"<now>"', body)
    body = re.sub(rb'"elapsed_ms":\s*\d+', b'"elapsed_ms":0', body)
    return re.sub(rb"\b[0-9a-f]{32}\b", b"<id>", body)


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


#: The Query tab's statements: rows and labels, a result past 500 rows, every kind of cell,
#: and each way a statement is refused.
STATEMENTS: tuple[object, ...] = (
    "SELECT item_id, title, year FROM items ORDER BY item_id LIMIT 20",
    "SELECT item_id FROM items WHERE type = 'episode' ORDER BY item_id LIMIT 5",
    "SELECT * FROM items",
    "SELECT x'00ff', 1e999, -1e999, 1e16, -0.0, 1e-5, 0.1 + 0.2, 2.0, NULL, 'héllo ☃', 7",
    "SELECT CAST(x'ff' AS TEXT) AS bad",
    "SELECT 1; -- done",
    "SELECT 1; SELECT 2",
    "SELECT ?",
    "DELETE FROM items",
    "PRAGMA table_info(items)",
    "ATTACH 'x.db' AS y",
    "CREATE TABLE t (x)",
    "SELEC 1",
    "SELECT nope FROM items",
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c",
    "",
    "   ",
    5,
)

QUERY_READS = ("/api/recipes", "/api/saved", "/api/dashboards")

#: The Query tab's writes, in order, as `(method, route, body, name)`: `name` keeps the id the
#: reply hands back (`dashboard.id` or `panel.id`) under that name, and `<name>` in a later
#: route or body is replaced by it, per server.
BAR = {"title": "Tracks", "chart": "bar", "value": "rows", "label": "lang", "number": "n"}
BAR_SQL = "SELECT lang, COUNT(*) AS n FROM media_stream GROUP BY lang"
QUERY_WRITES: tuple[tuple[str, str, object, str | None], ...] = (
    ("PUT", "/api/saved", {"name": "Parity B", "sql": "SELECT 1", "note": "n"}, None),
    ("PUT", "/api/saved", {"name": "parity a", "sql": "SELECT 2"}, None),
    ("PUT", "/api/saved", {"name": " Straße ", "sql": "SELECT 3", "note": None}, None),
    ("PUT", "/api/saved", {"name": "Parity B", "sql": "SELECT 4", "note": ""}, None),
    ("PUT", "/api/saved", {"name": "", "sql": "SELECT 1"}, None),
    ("PUT", "/api/saved", {"name": "x" * 201, "sql": "SELECT 1"}, None),
    ("PUT", "/api/saved", {"name": "x", "sql": "  "}, None),
    ("PUT", "/api/saved", {"name": "x", "sql": "SELECT 1", "note": 5}, None),
    ("DELETE", "/api/saved?name=parity%20a", None, None),
    ("DELETE", "/api/saved?name=nope", None, None),
    ("DELETE", "/api/saved", None, None),
    ("PUT", "/api/dashboards", {"name": "  Parity  "}, "D"),
    ("PUT", "/api/dashboards", {"id": "<D>", "name": "Parity renamed"}, None),
    ("PUT", "/api/dashboards", {"name": " "}, None),
    ("PUT", "/api/dashboards", {"name": "x" * 81}, None),
    ("PUT", "/api/dashboards", {"id": "a:b", "name": "x"}, None),
    ("PUT", "/api/dashboards", {"id": "nope", "name": "x"}, None),
    ("PUT", "/api/dashboards", {"id": 5, "name": "x"}, None),
    (
        "PUT",
        "/api/panels",
        {"dashboard_id": "<D>", "panel": {**BAR, "sql": BAR_SQL, "width": 2, "height": "tall"}},
        "P1",
    ),
    (
        "PUT",
        "/api/panels",
        {
            "dashboard_id": "<D>",
            "panel": {
                "title": "Count",
                "sql": "SELECT COUNT(*) AS n FROM items",
                "chart": "stat",
                "value": "column:n",
                "unit": "titles",
                "width": 1,
                "height": "short",
            },
        },
        "P2",
    ),
    (
        "PUT",
        "/api/panels",
        {
            "dashboard_id": "<D>",
            "panel": {
                **BAR,
                "id": "<P1>",
                "title": "Renamed",
                "sql": BAR_SQL,
                "width": 4,
                "height": "short",
            },
        },
        None,
    ),
    ("PUT", "/api/panels", {"dashboard_id": "<D>", "panel": "x"}, None),
    ("PUT", "/api/panels", {"dashboard_id": "<D>", "panel": {**BAR, "sql": " "}}, None),
    ("PUT", "/api/panels", {"dashboard_id": "<D>", "panel": {**BAR, "sql": "SELEC"}}, None),
    (
        "PUT",
        "/api/panels",
        {"dashboard_id": "<D>", "panel": {**BAR, "sql": BAR_SQL, "value": "column:zz"}},
        None,
    ),
    (
        "PUT",
        "/api/panels",
        {"dashboard_id": "<D>", "panel": {**BAR, "sql": BAR_SQL, "width": 9, "height": "tall"}},
        None,
    ),
    (
        "PUT",
        "/api/panels",
        {
            "dashboard_id": "<D>",
            "panel": {**BAR, "id": "nope", "sql": BAR_SQL, "width": 1, "height": "tall"},
        },
        None,
    ),
    (
        "PUT",
        "/api/panels",
        {"dashboard_id": "nope", "panel": {**BAR, "sql": BAR_SQL, "width": 1, "height": "tall"}},
        None,
    ),
    (
        "PUT",
        "/api/panels",
        {
            "dashboard_id": "<D>",
            "panel": {**BAR, "sql": BAR_SQL, "width": 1, "height": "tall", "label": 3},
        },
        None,
    ),
    (
        "PUT",
        "/api/panels",
        {
            "dashboard_id": "<D>",
            "panel": {**BAR, "id": "a:b", "sql": BAR_SQL, "width": 1, "height": "tall"},
        },
        None,
    ),
    ("POST", "/api/panels/move", {"dashboard_id": "<D>", "panel_id": "<P2>", "to": 0}, None),
    ("POST", "/api/panels/move", {"dashboard_id": "<D>", "panel_id": "<P2>", "to": 5}, None),
    ("POST", "/api/panels/move", {"dashboard_id": "<D>", "panel_id": "<P2>", "to": True}, None),
    ("POST", "/api/panels/move", {"dashboard_id": "<D>", "panel_id": "nope", "to": 0}, None),
    ("POST", "/api/panels/move", {"dashboard_id": 1, "panel_id": "<P2>", "to": 0}, None),
    ("DELETE", "/api/panels/<P1>", None, None),
    ("DELETE", "/api/panels/nope", None, None),
    ("DELETE", "/api/dashboards/<D>", None, None),
    ("DELETE", "/api/dashboards/nope", None, None),
    ("DELETE", "/api/dashboards/library-health", None, None),
)


def query_tab(python: str, rust: str) -> tuple[list[str], list[dict[str, str]]]:
    """Ask both servers for the Query tab's reads, statements and writes; returns the steps
    asked and the ones whose answers differ."""
    asked: list[str] = []
    differing: list[dict[str, str]] = []

    def check(label: str, py: tuple[int, str, bytes], rs: tuple[int, str, bytes]) -> None:
        asked.append(label)
        why = compare(label, py, rs)
        if why is not None:
            differing.append({"route": label[:200], "why": why})

    for route in QUERY_READS:
        check(route, fetch(python, route), fetch(rust, route))
    for sql in STATEMENTS:
        body = json.dumps({"sql": sql}).encode()
        check(
            f"POST /api/query {sql!r}",
            fetch(python, "/api/query", body, method="POST"),
            fetch(rust, "/api/query", body, method="POST"),
        )
    kept: dict[str, dict[str, str]] = {python: {}, rust: {}}

    def fill(text: str, base: str) -> str:
        for name, value in kept[base].items():
            text = text.replace(f"<{name}>", value)
        return text

    for method, route, body, name in QUERY_WRITES:
        answers = []
        for base in (python, rust):
            data = None if body is None else fill(json.dumps(body), base).encode()
            answer = fetch(base, fill(route, base), data, method=method)
            if name is not None and answer[0] == 200:
                reply = json.loads(answer[2])
                kept[base][name] = (reply.get("dashboard") or reply.get("panel"))["id"]
            answers.append(answer)
        check(f"{method} {route} {body!r}", answers[0], answers[1])
    for route in QUERY_READS:
        check(f"after the writes: {route}", fetch(python, route), fetch(rust, route))
    return asked, differing


# --- the Search tab -----------------------------------------------------------------------

FRAME_DIMS = 768
SEARCH_MODEL = "test/clip"
#: (title slot, shot in_ms, t_ms, angle from the phrase vector)
SEARCH_FRAMES = (
    (0, 0, 0, 0.0),
    (0, 0, 2000, 30.0),
    (0, 5000, 5000, 60.0),
    (1, 0, 0, 90.0),
    (1, 0, 2000, 10.0),
)
FILE_HEADERS = ("Accept-Ranges", "Content-Range", "Cache-Control")
#: What the fake Plex answers for each poster title's thumbnail:
#: (status, Content-Type or none, body bytes, Location or none).
POSTER_CASES = (
    (200, "image/jpeg", 2048, None),
    (200, None, 300, None),
    (200, "image/webp; charset=binary", 10, None),
    (204, "image/jpeg", 0, None),
    (404, "text/plain", 9, None),
    (500, "text/plain", 5, None),
    (302, "text/plain", 0, "/elsewhere"),
    (200, "image/png", 4 << 20, None),
    (200, "image/png", (4 << 20) + 1, None),
)


def _serve_json(handler_class: type) -> str:
    import http.server
    import threading

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


def search_fixture(
    root: Path, store: Path, rs_bin: str, homes: dict[str, Path]
) -> dict[str, object]:
    """Seed the Search tab's world and return what the servers and the requests need."""
    import http.server
    import math
    import sqlite3

    import sqlite_vec
    from tokenizers import Tokenizer, models, pre_tokenizers

    with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as conn:
        titles = conn.execute(
            "SELECT i.item_id, p.rating_key, p.section_id FROM items i "
            "JOIN plex_items p USING (item_id) "
            "WHERE i.type = 'movie' ORDER BY i.item_id LIMIT 3"
        ).fetchall()
        collection = conn.execute(
            "SELECT collection_id FROM collection ORDER BY collection_id LIMIT 1"
        ).fetchone()
        poster_ids = [
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT item_id FROM plex_items ORDER BY item_id LIMIT ? OFFSET 100",
                (len(POSTER_CASES),),
            )
        ]
        # The rating key `/api/poster` resolves each title to.
        poster_keys = [
            str(
                conn.execute(
                    "SELECT rating_key FROM plex_items WHERE item_id = ? "
                    "ORDER BY last_seen DESC LIMIT 1",
                    (i,),
                ).fetchone()[0]
            )
            for i in poster_ids
        ]
    if len(titles) < 3:
        raise SystemExit("error: the store holds fewer than three movies with Plex rating keys")
    if len(poster_ids) < len(POSTER_CASES):
        raise SystemExit("error: the store holds too few titles with Plex rating keys")
    thumbs = {
        f"/library/metadata/{key}/thumb": case
        for key, case in zip(poster_keys, POSTER_CASES, strict=True)
    }
    ids = [str(t[0]) for t in titles]
    keys = [str(t[1]) for t in titles]
    sections = sorted({str(t[2]) for t in titles})

    frames_dir = root / "frames"
    subprocess.run(
        [rs_bin, "frames", "migrate"],
        env={**os.environ, "PLEXDB_FRAMES_DIR": str(frames_dir)},
        check=True,
        capture_output=True,
    )
    frames = sqlite3.connect(frames_dir / "frames.db")
    frames.enable_load_extension(True)
    sqlite_vec.load(frames)
    for slot, part in ((0, 11), (1, 12)):
        frames.execute(
            "INSERT INTO frame_index_state (item_id, part_id, model, model_version, dims, quant, "
            "duration_ms, vectors, indexed_at) "
            "VALUES (?, ?, ?, 'v1', ?, 'int8', 10000, 3, '2026-01-02T03:04:05+00:00')",
            (ids[slot], part, SEARCH_MODEL, FRAME_DIMS),
        )
    starts: dict[str, list[int]] = {}
    for slot, shot_in, t_ms, degrees in SEARCH_FRAMES:
        item_id = ids[slot]
        ins = starts.setdefault(item_id, [])
        if shot_in not in ins:
            ins.append(shot_in)
            frames.execute(
                "INSERT INTO shots (item_id, shot_ix, in_ms, out_ms) VALUES (?, ?, ?, ?)",
                (item_id, len(ins) - 1, shot_in, shot_in + 5000),
            )
        angle = math.radians(degrees)
        vec = [round(127 * math.cos(angle)), round(127 * math.sin(angle))] + [0] * (FRAME_DIMS - 2)
        frames.execute(
            "INSERT INTO frame_vec (item_id, t_ms, shot_ix, vec) VALUES (?, ?, ?, vec_int8(?))",
            (item_id, t_ms, ins.index(shot_in), json.dumps(vec)),
        )
    frames.commit()
    frames.close()
    # Each server writes its own frames.db (`queue`) and Phrase cache.
    shutil.copytree(frames_dir, homes["rust"] / "frames")

    tokenizer = Tokenizer(
        models.WordPiece(
            {
                "[UNK]": 0,
                "fire": 1,
                "##fight": 2,
                "sunny": 3,
                "in": 4,
                "los": 5,
                "angeles": 6,
                "a": 7,
                "dog": 8,
                "é": 9,
            },
            unk_token="[UNK]",
        )
    )
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    # Outside both homes: the end-of-run file comparison looks at every `*.json` in them.
    tokenizer_file = root / "fixture" / "tokenizer.json"
    tokenizer_file.parent.mkdir()
    tokenizer.save(str(tokenizer_file))

    media = root / "media"
    media.mkdir()
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=3:size=64x48:rate=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(media / "heat.mp4"),
        ],
        check=True,
    )

    unit = [1.0] + [0.0] * (FRAME_DIMS - 1)

    class Encoder(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}"
            )
            texts = body.get("input") or []
            if "refuse me" in texts:
                self._send(500, {"error": "no model loaded"})
                return
            self._send(200, {"data": [{"index": i, "embedding": unit} for i in range(len(texts))]})

        def _send(self, status: int, body: dict[str, object]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_: object) -> None:
            pass

    class Plex(Encoder):
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            if path in thumbs:
                self._thumb(thumbs[path])
                return
            if path.startswith("/library/sections"):
                body = {
                    "MediaContainer": {
                        "Directory": [
                            {"key": s, "type": "movie", "title": f"Library {s}"} for s in sections
                        ]
                    }
                }
            elif path == f"/library/metadata/{keys[0]}":
                body = {
                    "MediaContainer": {
                        "Metadata": [
                            {
                                "ratingKey": keys[0],
                                "Media": [{"Part": [{"id": 11, "file": "/library/heat.mp4"}]}],
                            }
                        ]
                    }
                }
            else:
                body = {"MediaContainer": {"Metadata": []}}
            self._send(200, body)

        def _thumb(self, case: tuple[int, str | None, int, str | None]) -> None:
            status, content_type, size, location = case
            if self.headers.get("X-Plex-Token") != "parity":
                status, content_type, size, location = 401, "text/plain", 4, None
            self.send_response(status)
            if content_type is not None:
                self.send_header("Content-Type", content_type)
            if location is not None:
                self.send_header("Location", location)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            self.wfile.write(bytes(i % 251 for i in range(size)))

    env = {
        "PLEXDB_FRAME_ENCODER_URL": _serve_json(Encoder),
        "PLEXDB_FRAME_MODEL": SEARCH_MODEL,
        "PLEXDB_FRAME_TOKENIZER": str(tokenizer_file),
        "PLEX_URL": _serve_json(Plex),
        "PLEX_TOKEN": "parity",
        "PLEXDB_MEDIA_DIR": str(media),
        "PLEX_SOURCE_ROOTS": "/library",
    }
    return {
        "ids": ids,
        "poster_ids": poster_ids,
        "collection": collection[0] if collection else "nope",
        "env": env,
    }


def close_json(a: object, b: object, tolerance: float = 1e-5) -> bool:
    """Equal, with every number within `tolerance` (absolute or relative), lists in order."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= tolerance * max(1.0, abs(a), abs(b))
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(
            close_json(x, y, tolerance) for x, y in zip(a, b, strict=True)
        )
    if isinstance(a, dict) and isinstance(b, dict):
        return list(a) == list(b) and all(close_json(a[k], b[k], tolerance) for k in a)
    return a == b


def fetch_full(
    base: str, route: str, headers: dict[str, str] | None = None
) -> tuple[int, str, bytes, dict[str, str]]:
    request = urllib.request.Request(base + route, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
            return (
                reply.status,
                reply.headers.get("Content-Type", ""),
                reply.read(),
                {h: reply.headers.get(h, "") for h in FILE_HEADERS},
            )
    except urllib.error.HTTPError as err:
        return (
            err.code,
            err.headers.get("Content-Type", ""),
            err.read(),
            {h: err.headers.get(h, "") for h in FILE_HEADERS},
        )


def search_tab(
    python: str, rust: str, fixture: dict[str, object]
) -> tuple[list[str], list[dict[str, str]]]:
    """Ask both servers the Search tab's routes; the steps asked and the ones that differ."""
    ids: list[str] = fixture["ids"]  # type: ignore[assignment]
    t1, t2, t3 = (quote(i) for i in ids)
    cid = quote(str(fixture["collection"]))
    asked: list[str] = []
    differing: list[dict[str, str]] = []

    def check(
        label: str, py: tuple[int, str, bytes], rs: tuple[int, str, bytes], *, close: bool = False
    ) -> None:
        asked.append(label)
        if close and py[0] == rs[0] == 200:
            why = (
                None
                if close_json(json.loads(py[2]), json.loads(rs[2]))
                else "parsed JSON differs past 1e-5"
            )
        else:
            why = compare(label, py, rs)
        if why is not None:
            differing.append({"route": label[:200], "why": why})

    def get(route: str) -> None:
        check(route, fetch(python, route), fetch(rust, route))

    def post(route: str, body: object, *, close: bool = False) -> None:
        data = json.dumps(body).encode()
        check(
            f"POST {route} {json.dumps(body)[:120]}",
            fetch(python, route, data, method="POST"),
            fetch(rust, route, data, method="POST"),
            close=close,
        )

    def file(route: str, range_header: str | None = None) -> None:
        headers = {"Range": range_header} if range_header else {}
        label = f"{route} Range={range_header!r}"
        py = fetch_full(python, route, headers)
        rs = fetch_full(rust, route, headers)
        asked.append(label)
        why = compare(label, py[:3], rs[:3])
        if why is None and py[3] != rs[3]:
            why = f"headers {py[3]} in Python, {rs[3]} in Rust"
        if why is not None:
            differing.append({"route": label[:200], "why": why})

    for route in (
        "/api/search/status",
        "/api/search/frames",
        "/api/search/indexed",
        "/api/search/suggest?q=he",
        "/api/search/suggest?q=h",
        "/api/search/suggest?q=%20%20",
        "/api/search/suggest",
        f"/api/search/collection?id={cid}",
        "/api/search/collection?id=nope",
        "/api/search/collection",
    ):
        get(route)
    post("/api/search/tokenize", {"texts": ["sunny firefight é", "a " * 76, ""]})
    post("/api/search/tokenize", {"texts": "x"})
    post("/api/search/tokenize", {"texts": ["x" * 4001]})
    post("/api/search/pool", {"item_ids": [ids[0], ids[1], "nope", ids[0], ids[2]]})
    post("/api/search/pool", {"item_ids": []})
    post("/api/search/pool", {"item_ids": "x"})
    post("/api/search/queue", {"item_ids": [ids[2], "nope"]})
    post("/api/search/pool", {"item_ids": [ids[2]]})
    get("/api/search/frames")
    run = {
        "phrase": "sunny firefight",
        "pool": ids,
        "negatives": ["dog"],
        "weight": 0,
        "temperature": 0.5,
        "draw": {"n": 1, "seed": 3},
    }
    post("/api/search/run", run)
    post(
        "/api/search/run",
        {"phrase": "  sunny firefight  ", "negatives": ["dog", ""], "weight": 1.5},
    )
    post("/api/search/run", {"phrase": "sunny firefight", "pool": [ids[2]]})
    for bad in (
        {"phrase": "--help", "pool": ids[:1]},
        {"phrase": "x", "temperature": 0},
        {"phrase": "x", "draw": {"n": 0}},
        {"phrase": "x", "draw": {"n": 2.0}},
        {"phrase": "x", "draw": []},
        {"phrase": "x", "weight": "heavy"},
        {"phrase": "x", "weight": True},
        {"phrase": "x", "weight": 11},
        {"phrase": ""},
        {"phrase": 5},
        {},
        {"phrase": "x", "negatives": ["n"] * 21},
        {"phrase": "x", "negatives": [1]},
        {"phrase": "x", "pool": "p"},
        {"phrase": "x", "pool": ["a" * 201]},
        {"phrase": "x" * 4001},
        {"phrase": "refuse me", "pool": ids[:1]},
    ):
        post("/api/search/run", bad)
    post(
        "/api/search/why", {"phrase": "sunny firefight", "item_id": ids[0], "in_ms": 0}, close=True
    )
    post(
        "/api/search/why", {"phrase": "sunny firefight", "item_id": ids[1], "in_ms": 0}, close=True
    )
    for bad in (
        {"phrase": "sunny firefight", "item_id": ids[0], "in_ms": 7},
        {"phrase": "nobody searched this", "item_id": ids[0], "in_ms": 0},
        {"item_id": ids[0], "in_ms": 0},
        {"phrase": "x", "in_ms": 0},
        {"phrase": "x", "item_id": ids[0], "in_ms": -1},
        {"phrase": "x", "item_id": ids[0], "in_ms": 1.5},
        {"phrase": "x", "item_id": ids[2], "in_ms": 0},
    ):
        post("/api/search/why", bad)
    clip = f"/api/search/clip?item_id={t1}&part=11&in_ms=0&out_ms=2000"
    file(f"/api/search/frame?item_id={t1}&part=11&t_ms=1000")
    file(f"/api/search/frame?item_id={t1}&part=11&t_ms=1000&large=1")
    file(clip)
    for range_header in (
        "bytes=4-7",
        "bytes=-10",
        "bytes=-0",
        "bytes=-",
        "bytes=0-3,5-6",
        "bytes=10-",
        "bytes=x-y",
        "items=1-2",
        "bytes=100000000-",
    ):
        file(clip, range_header)
    for route in (
        f"/api/search/frame?item_id={t1}&part=10&t_ms=1000",
        f"/api/search/frame?item_id={t3}&part=0&t_ms=1000",
        f"/api/search/frame?item_id={t1}&part=11&t_ms=20000",
        f"/api/search/frame?item_id={t2}&part=12&t_ms=0",
        f"/api/search/clip?item_id={t1}&part=11&in_ms=0&out_ms=200000",
        f"/api/search/clip?item_id={t1}&part=11&in_ms=5&out_ms=5",
        f"/api/search/clip?item_id={t1}&part=11&in_ms=x&out_ms=5",
        f"/api/search/clip?item_id={t1}&part=11&in_ms=1&out_ms=1234567890123",
        "/api/search/frame?part=11&t_ms=5",
        "/api/search/frame?item_id=" + "x" * 201 + "&part=1&t_ms=5",
        "/api/search/nope",
    ):
        file(route)
    return asked, differing


def poster_routes(fixture: dict[str, object]) -> list[str]:
    """Every `/api/poster` request to ask: each fake-Plex case twice (the second is a cache hit
    or, for a refusal, a second fetch), titles with no rating key, and the bad requests."""
    ids: list[str] = fixture["poster_ids"]  # type: ignore[assignment]
    asked = [f"/api/poster?item_id={quote(i)}" for i in ids for _ in range(2)]
    asked += [
        "/api/poster?item_id=nope",
        "/api/poster?item_id=" + quote("it's"),
        "/api/poster?item_id=" + quote("a \"b\" 'c'"),
        "/api/poster",
        "/api/poster?item_id=",
        "/api/poster?item_id=" + "x" * 201,
        "/api/poster?item_id=" + "x" * 200,
        "/api/poster/",
    ]
    return asked


def poster_tab(python: str, rust: str, routes: list[str]) -> list[dict[str, str]]:
    """Ask both servers each poster route; status, Content-Type, body and Cache-Control."""
    differing: list[dict[str, str]] = []
    for route in routes:
        py = fetch_full(python, route)
        rs = fetch_full(rust, route)
        why = compare(route, py[:3], rs[:3])
        if why is None and py[3] != rs[3]:
            why = f"headers {py[3]} in Python, {rs[3]} in Rust"
        if why is not None:
            differing.append({"route": route[:200], "why": why})
    return differing


def closed_port() -> str:
    """A local URL nothing listens on."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


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
    read_bin = Path(rs_bin).with_name("plexdb-read")
    if not read_bin.is_file():
        # The Python Search tab runs the reader crate's binary; the same build dir carries it.
        subprocess.run(
            [
                "cargo",
                "build",
                "-q",
                "--release",
                "--manifest-path",
                str(REPO / "Cargo.toml"),
                "-p",
                "plexdb-reader",
                "--bin",
                "plexdb-read",
            ],
            env={**os.environ, "CARGO_TARGET_DIR": str(read_bin.parent.parent)},
            check=True,
        )
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
        fixture = search_fixture(root, store, rs_bin, homes)
        env = {
            **os.environ,
            "PLEXDB_PATH": str(root / "plexdb.db"),
            "PLEXDB_READ_BIN": str(read_bin),
        }
        env.pop("PLEXDB_SNAPSHOT_PATH", None)
        env.pop("PLEXDB_PHRASE_CACHE", None)
        env.update(fixture["env"])  # type: ignore[arg-type]
        env["PLEXDB_FRAMES_DIR"] = str(root / "frames")
        rust_env = {
            **env,
            "PLEXDB_EXPLORE_SAVED_PATH": str(homes["rust"] / "explore-queries.json"),
            "PLEXDB_FRAMES_DIR": str(homes["rust"] / "frames"),
        }
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
            query_asked, query_differing = query_tab(python, rust)
            asked += query_asked
            differing += query_differing
            search_asked, search_differing = search_tab(python, rust, fixture)
            asked += search_asked
            differing += search_differing
            posters = poster_routes(fixture)
            asked += posters
            differing += poster_tab(python, rust, posters)
        # Plex unreachable, then not configured: a server pair of each, asked one poster.
        one = [f"/api/poster?item_id={quote(fixture['poster_ids'][0])}"]  # type: ignore[index]
        for label, plex_url in (("unreachable", closed_port()), ("unset", "")):
            pair_env = {**env, "PLEX_URL": plex_url}
            with (
                serve(
                    [sys.executable, "-m", "plexdb.cli", "explore", "--port", "0"], pair_env
                ) as py,
                serve([rs_bin, "explore", "--port", "0"], {**rust_env, "PLEX_URL": plex_url}) as rs,
            ):
                asked += [f"Plex {label}: {r}" for r in one]
                differing += [
                    {**d, "route": f"Plex {label}: {d['route']}"} for d in poster_tab(py, rs, one)
                ]
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
