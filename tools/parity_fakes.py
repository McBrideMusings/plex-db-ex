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


# --- harvest-frames --------------------------------------------------------------

#: Seconds of `(colour, length)` each fixture video plays, as `ffmpeg -f lavfi` sources.
#: A hard colour change is a scene cut for scdet; the 0.3 s runs sit closer together
#: than `MIN_SHOT_MS`, so the sampler merges their boundaries while the store keeps them.
_VIDEOS: dict[str, list[tuple[str, float]]] = {
    "three-scenes": [("red", 4.0), ("blue", 5.0), ("green", 3.0)],
    "quick-flashes": [("red", 2.0), ("blue", 0.3), ("green", 0.3), ("yellow", 6.0)],
}
_CHAPTERS = (0, 4000, 9000)


def _make_video(
    ffmpeg: str, target: Path, scenes: list[tuple[str, float]], *, chapters: tuple[int, ...] = ()
) -> None:
    """A small H.264 file whose audio outlasts its video by two seconds, with a chapter
    starting at each of `chapters` (milliseconds) when given."""
    import subprocess

    argv = [ffmpeg, "-nostdin", "-v", "error"]
    for colour, length in scenes:
        argv += ["-f", "lavfi", "-i", f"color=c={colour}:s=320x180:r=10:d={length}"]
    total = sum(length for _, length in scenes)
    argv += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={total + 2}"]
    meta = target.with_suffix(".ffmeta")
    if chapters:
        lines = [";FFMETADATA1"]
        edges = [*chapters, round(total * 1000)]
        for start, end in zip(edges, edges[1:], strict=False):
            lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}", "title=c"]
        meta.write_text("\n".join(lines) + "\n", encoding="utf-8")
        argv += ["-i", str(meta), "-map_metadata", str(len(scenes) + 1)]
    count = len(scenes)
    argv += [
        "-filter_complex",
        "".join(f"[{i}:v]" for i in range(count)) + f"concat=n={count}:v=1:a=0[v]",
        "-map", "[v]", "-map", f"{count}:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-y", str(target),
    ]  # fmt: skip
    subprocess.run(argv, check=True, capture_output=True)


def _frame_vector(uri: str) -> list[float]:
    """A 768-number vector for one image, a pure function of its bytes."""
    import hashlib
    import random

    rng = random.Random(hashlib.sha256(uri.encode()).digest())
    return [round(rng.uniform(-1, 1), 6) for _ in range(768)]


class FramesScenario:
    """What a `harvest-frames` run is given: a media directory of fixture videos, a Plex
    listing them, and a Frame index seeded with the states each discovery rule acts on.
    `requests` holds one line per Plex path asked and per batch embedded."""

    def __init__(self, media_dir: Path, store: Path) -> None:
        self.media_dir = media_dir
        self.requests: list[str] = []
        self.lock = threading.Lock()
        self.titles: list[dict[str, object]] = []
        self.roles: dict[str, str] = {}
        self._build(store)

    def note(self, line: str) -> None:
        with self.lock:
            self.requests.append(line)

    def seed(self, conn: sqlite3.Connection) -> None:
        pass

    def _build(self, store: Path) -> None:
        import os

        ffmpeg = os.environ.get("PLEXDB_FFMPEG", "ffmpeg")
        media_dir = self.media_dir
        movies = media_dir / "movies"
        shows = media_dir / "shows"
        movies.mkdir(parents=True)
        shows.mkdir()
        with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as conn:
            picked: dict[str, list[tuple[str, str, str]]] = {"movie": [], "episode": []}
            for kind, count in (("movie", 9), ("episode", 2)):
                for rating_key, item_id, section in conn.execute(
                    "SELECT p.rating_key, p.item_id, p.section_id FROM plex_items p "
                    "JOIN items i ON i.item_id = p.item_id WHERE i.type = ? "
                    "GROUP BY p.item_id HAVING count(*) = 1 ORDER BY p.item_id LIMIT ?",
                    (kind, count),
                ):
                    picked[kind].append((str(rating_key), str(item_id), str(section)))
        if len(picked["movie"]) < 9 or len(picked["episode"]) < 2:
            raise SystemExit("parity: the store holds too few titles with one Plex copy each")
        _make_video(ffmpeg, movies / "scenes.mkv", _VIDEOS["three-scenes"], chapters=())
        _make_video(ffmpeg, movies / "chapters.mkv", _VIDEOS["three-scenes"], chapters=_CHAPTERS)
        _make_video(ffmpeg, movies / "flashes.mkv", _VIDEOS["quick-flashes"], chapters=())
        (movies / "damaged.mkv").write_bytes(b"this is not a video\n" * 100)
        roles = [
            # (role, file, added_at)
            ("new", "movies/scenes.mkv", 500),
            ("new with chapters", "movies/chapters.mkv", 600),
            ("file missing", "movies/missing.mkv", 550),
            ("file damaged", "movies/damaged.mkv", 450),
            ("queued", "movies/flashes.mkv", 100),
            ("part changed", "movies/scenes.mkv", 300),
            ("file changed", "movies/chapters.mkv", 200),
            ("unchanged", "movies/flashes.mkv", 150),
            ("new, same added_at", "movies/flashes.mkv", 500),
        ]
        for ix, (rating_key, item_id, section) in enumerate(picked["movie"]):
            role, file, added = roles[ix]
            self._add(rating_key, item_id, section, "movie", role, file, added, 1000 + ix)
        for ix, (rating_key, item_id, section) in enumerate(picked["episode"]):
            self._add(
                rating_key, item_id, section, "show", f"new episode {ix}",
                "shows/pilot.mkv", 700 - 600 * ix, 2000 + ix,
            )  # fmt: skip
        _make_video(ffmpeg, shows / "pilot.mkv", _VIDEOS["quick-flashes"], chapters=(0, 3000, 6000))

    def _add(  # noqa: PLR0913
        self, rating_key: str, item_id: str, section: str, section_type: str, role: str,
        file: str, added_at: int, part_id: int,
    ) -> None:  # fmt: skip
        self.roles[item_id] = role
        local = self.media_dir / file
        size = local.stat().st_size if local.exists() else 123456
        self.titles.append(
            {
                "item_id": item_id, "section": section, "section_type": section_type,
                "record": {
                    "ratingKey": rating_key, "addedAt": added_at,
                    "Media": [{"Part": [{"id": part_id, "size": size, "file": f"/media/{file}"}]}],
                },
            }
        )  # fmt: skip

    def seed_frames(self, frames_db: Path) -> None:
        """The Frame index one side starts from: a queued title, a title indexed from a part
        Plex has since replaced, one whose file changed, and one that has not."""
        from datetime import datetime

        from plexdb import frames
        from plexdb.frame_harvest import FfmpegMedia

        media = FfmpegMedia(ffmpeg="ffmpeg", ffprobe="ffprobe")
        by_role = {self.roles[str(t["item_id"])]: t for t in self.titles}
        stamp = datetime(2026, 1, 1).astimezone().isoformat()

        def part(title: dict[str, object]) -> dict[str, object]:
            record = title["record"]
            assert isinstance(record, dict)
            return record["Media"][0]["Part"][0]  # type: ignore[no-any-return]

        with frames.open_frames(frames_db) as conn:
            queued = by_role["queued"]["item_id"]
            conn.execute(
                "INSERT INTO frame_queue (item_id, queued_at, queued_by) VALUES (?, ?, 'parity')",
                (queued, stamp),
            )
            rows = [
                (by_role["part changed"], 99999, "whatever", "2026-01-02T00:00:00+00:00"),
                (by_role["file changed"], part(by_role["file changed"])["id"], "stale", stamp),
            ]
            same = by_role["unchanged"]
            current = media.file_hash(
                self.media_dir / str(part(same)["file"]).removeprefix("/media/"),
                int(part(same)["size"]),  # type: ignore[call-overload]
            )
            rows.append((same, part(same)["id"], current, "2026-01-03T00:00:00+00:00"))
            for title, part_id, file_hash, indexed_at in rows:
                conn.execute(
                    "INSERT INTO frame_index_state (item_id, part_id, file_hash, indexed_at) "
                    "VALUES (?, ?, ?, ?)",
                    (title["item_id"], part_id, file_hash, indexed_at),
                )
            conn.commit()


def _frames_handlers(scenario: FramesScenario) -> type[BaseHTTPRequestHandler]:
    from urllib.parse import parse_qsl, urlsplit

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

        def _send(self, status: int, body: object) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)

def _plex_accounts(store: Path) -> list[dict[str, object]]:
    """Plex's `/accounts` for `store`: a name for most accounts that have plays, a blank
    one for every third, Plex's id-0 placeholder, and an account with no plays."""
    with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as conn:
        ids = [row[0] for row in conn.execute("SELECT DISTINCT plex_account_id FROM plays")]
    accounts: list[dict[str, object]] = [{"id": 0, "name": ""}]
    for ix, account_id in enumerate(sorted(ids)):
        name = "" if ix % 3 == 2 else f"Viewer {account_id} é"
        accounts.append({"id": account_id, "name": name})
    accounts.append({"id": 999999999, "name": "Nobody Watched"})
    return accounts


@contextlib.contextmanager
def serve_plex(store: Path) -> Iterator[dict[str, str]]:
    """A Plex-shaped server answering `/accounts` only, and the environment pointing a
    step at it. A request without the token is refused, as Plex does."""
    body = json.dumps({"MediaContainer": {"Account": _plex_accounts(store)}}).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

        def do_GET(self) -> None:  # noqa: N802
            known = self.path == "/accounts" and self.headers.get("X-Plex-Token") == "parity-token"
            raw = body if known else b"{}"
            self.send_response(200 if known else 401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            if parts.path == "/library/sections":
                seen = {(str(t["section"]), str(t["section_type"])) for t in scenario.titles}
                scenario.note("GET /library/sections")
                self._send(
                    200,
                    {
                        "MediaContainer": {
                            "Directory": [
                                {"key": key, "type": kind, "title": f"Section {key}"}
                                for key, kind in sorted(seen)
                            ]
                        }
                    },
                )
                return
            prefix, suffix = "/library/sections/", "/all"
            if parts.path.startswith(prefix) and parts.path.endswith(suffix):
                key = parts.path[len(prefix) : -len(suffix)]
                wanted = "movie" if query.get("type") == "1" else "episode"
                scenario.note(f"GET {parts.path} type={query.get('type')}")
                records = [
                    t["record"]
                    for t in scenario.titles
                    if t["section"] == key and (t["section_type"] == "movie") == (wanted == "movie")
                ]
                self._send(
                    200,
                    {"MediaContainer": {"totalSize": len(records), "Metadata": records}},
                )
                return
            self._send(404, {})

        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path != "/v1/embeddings":
                self._send(404, {})
                return
            inputs = body["input"]
            scenario.note(f"embed {len(inputs)} {body['modality']} {body['model']}")
            rows = [{"index": i, "embedding": _frame_vector(uri)} for i, uri in enumerate(inputs)]
            self._send(200, {"data": rows[::-1], "model": "fake-clip-parity"})

    return Handler


@contextlib.contextmanager
def serve_frames(store: Path) -> Iterator[tuple[dict[str, str], FramesScenario]]:
    """A Plex listing fixture videos, an encoder answering a vector per image, and the
    environment that points `harvest-frames` at them and at the videos' directory.
    Both implementations read the same files, which the fake leaves untouched."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="plexdb-parity-media.") as tmp:
        scenario = FramesScenario(Path(tmp) / "media", store)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _frames_handlers(scenario))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            yield (
                {
                    "PLEX_URL": base,
                    "PLEX_TOKEN": "parity-token",
                    "PLEX_SOURCE_ROOTS": "/media",
                    "PLEXDB_MEDIA_DIR": str(scenario.media_dir),
                    "PLEXDB_FRAME_ENCODER_URL": base,
                    "PLEXDB_FRAME_MODEL": "fake-clip",
                    "PLEXDB_FRAME_HOURS": "",
                    "NO_PROXY": "127.0.0.1",
                    "no_proxy": "127.0.0.1",
                },
                scenario,
            )
        finally:
            server.shutdown()
            server.server_close()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "PLEX_URL": f"http://127.0.0.1:{server.server_port}",
            "PLEX_TOKEN": "parity-token",
            "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1",
        }
    finally:
        server.shutdown()
        server.server_close()
