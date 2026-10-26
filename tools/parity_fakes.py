"""Fake external servers for `admin parity`, so a fetching step runs offline and
both implementations see byte-identical answers.

A fetching step asks the network, and the network changes between two runs and
spends a quota. Each fake here answers from a pure function of the request path
(and of the store, where an answer names other titles), so the Python run and the
Rust run read the same world. A fake binds 127.0.0.1 on a free port; the step is
pointed at it through the variable its client reads for a base URL.

    with serve_tmdb(store) as env:   # env: TMDB_BASE_URL, TMDB_API_KEY, NO_PROXY
        ...run both sides with env...
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import threading
import zlib
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

#: Keyword names a title may carry. Spellings repeat on purpose: plural and
#: hyphenated forms fold onto one stored value, two spellings of one value arrive
#: in one response, and a blank name is dropped.
_KEYWORDS = [
    "heist",
    "Heists",
    "bank-heist",
    "Time Travel",
    "time-travel",
    "superheroes",
    "women's rights",
    "Women’s Rights",
    "coming of age",
    "based on novel or book",
    "Christmas",
    "boxing",
    "murder",
    "murders",
    "alps",
    "1970s",
    "c#",
    "(soccer)",
    "d.c.",
    "españa",
    "mother-daughter relationship",
    "spaceships",
    "Zombies!",
    "&",
]


def _seed(*parts: object) -> int:
    return zlib.crc32("|".join(map(str, parts)).encode())


def _keywords(media: str, tmdb_id: str) -> list[dict[str, object]]:
    seed = _seed(media, tmdb_id)
    if seed % 7 == 0:
        return []
    names = [_KEYWORDS[(seed >> shift) % len(_KEYWORDS)] for shift in range(0, 20, 4)]
    if seed % 11 == 0:
        names.append("")
    return [{"id": 1000 + i, "name": name} for i, name in enumerate(names[: 1 + seed % 5])]


def _related(media: str, tmdb_id: str, endpoint: str, known: list[str]) -> list[dict[str, object]]:
    seed = _seed(media, tmdb_id, endpoint)
    if seed % 5 == 0 or not known:
        return []
    picks = [known[(seed >> shift) % len(known)] for shift in range(0, 24, 3)]
    picks.insert(seed % 4, tmdb_id)  # a title recommending itself
    picks.insert(seed % 3, "999999999")  # a title nobody walked
    picks.append(picks[0])  # a repeat
    return [{"id": int(p)} for p in picks if p.isdigit()]


class _Handler(BaseHTTPRequestHandler):
    known: list[str] = []
    key = ""
    requests = 0

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def _send(self, status: int, body: object) -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        type(self).requests += 1
        path, _, query = self.path.partition("?")
        if f"api_key={self.key}" not in query.split("&"):
            self._send(401, {"status_message": "Invalid API key"})
            return
        match = re.fullmatch(r"/(movie|tv)/(\d+)(?:/(keywords|recommendations|similar))?", path)
        if not match:
            self._send(404, {"status_message": "not found"})
            return
        media, tmdb_id, endpoint = match.groups()
        seed = _seed(media, tmdb_id)
        if seed % 211 == 0:
            self._send(500, {"status_message": "internal error"})
        elif seed % 89 == 3:
            self._send(404, {"status_code": 34, "status_message": "not found"})
        elif seed % 233 == 5:
            self._send(200, b"<html>not json</html>")
        elif endpoint == "keywords":
            field = "keywords" if media == "movie" else "results"
            self._send(200, {"id": int(tmdb_id), field: _keywords(media, tmdb_id)})
        elif endpoint is None:
            language = ["en", "ja", "", "fr", "ko", None][seed % 6]
            body: dict[str, object] = {"id": int(tmdb_id)}
            if language is not None:
                body["original_language"] = language
            self._send(200, body)
        else:
            results = _related(media, tmdb_id, endpoint, self.known)
            self._send(200, {"page": 1, "results": results, "total_results": len(results)})


@contextlib.contextmanager
def serve_tmdb(store: Path) -> Iterator[dict[str, str]]:
    """A TMDB-shaped server over `store`'s TMDB ids, and the environment pointing a
    step at it."""
    with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as conn:
        known = sorted(
            {row[0] for row in conn.execute("SELECT value FROM external_ids WHERE ns = 'tmdb'")}
        )
    handler = type("Handler", (_Handler,), {"known": known, "key": "parity-key", "requests": 0})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "TMDB_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            "TMDB_API_KEY": "parity-key",
            "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1",
        }
    finally:
        server.shutdown()
        server.server_close()


#: Roles Jev scores for a tag, in the order `keywords.ROLES` lists them.
_ROLES = ("tone", "era", "region", "theme", "character_trait")


def _vector(text: str) -> list[float]:
    """A 16-number vector for `text`. Texts that start with the same word share most of
    it, so some pairs of keywords land above the cosine cut and most do not."""
    head = text.split(" ")[0]
    return [
        round(
            (_seed("head", head, i) % 2001 - 1000) / 1000
            + 0.45 * ((_seed("tail", text, i) % 2001 - 1000) / 1000),
            6,
        )
        for i in range(16)
    ]


class JevScenario:
    """The requests a Jev-shaped fake received, as one line each, and nothing to seed."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.lock = threading.Lock()

    def seed(self, conn: sqlite3.Connection) -> None:
        pass

    def note(self, line: str) -> None:
        with self.lock:
            self.requests.append(line)


def _jev_handler(scenario: JevScenario, key: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

        def _send(self, status: int, body: object) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/v1/embeddings":
                texts = body["input"]
                scenario.note("embed " + json.dumps(texts, ensure_ascii=False))
                rows = [{"index": i, "embedding": _vector(text)} for i, text in enumerate(texts)]
                self._send(200, {"data": rows[::-1]})
                return
            if self.path != "/v1/systemone":
                self._send(404, {})
                return
            if self.headers.get("Authorization") != f"Bearer {key}":
                self._send(401, {})
                return
            state = body["state"]
            if "tag_a" in state:
                scenario.note(f"judge {state['tag_a']} | {state['tag_b']}")
                seed = _seed("pair", state["tag_a"], state["tag_b"])
                if seed % 17 == 0:
                    self._send(422, {})
                    return
                answers = {"n": {"noul": (seed % 101) / 100}, "s": {"score": seed % 3}}
            else:
                scenario.note(f"roles {state['tag']}")
                seed = _seed("roles", state["tag"])
                if seed % 13 == 0:
                    self._send(400, {})
                    return
                answers = {
                    role: {"noul": (_seed(role, state["tag"]) % 1001) / 1000} for role in _ROLES
                }
            self._send(200, {"answers": answers, "model": "jev-parity-1.0"})

    return Handler


@contextlib.contextmanager
def serve_jev(store: Path) -> Iterator[tuple[dict[str, str], JevScenario]]:
    """A server answering the embedding endpoint and Jev's, and the environment that
    points the judge steps at it. Requests arrive from several threads, so the scenario
    keeps them as lines to compare sorted."""
    del store
    scenario = JevScenario()

    class Server(ThreadingHTTPServer):
        #: The judges send eight requests at once; the default backlog of five drops some.
        request_queue_size = 128

    server = Server(("127.0.0.1", 0), _jev_handler(scenario, "parity-key"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield (
            {
                "LLAMA_BROKER_BASE_URL": base,
                "JEV_URL": f"{base}/v1/systemone",
                "TYPESAFE_API_KEY": "parity-key",
                "NO_PROXY": "127.0.0.1",
                "no_proxy": "127.0.0.1",
            },
            scenario,
        )
    finally:
        server.shutdown()
        server.server_close()
