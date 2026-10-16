"""The Search tab's server side: the Frame index, searched from Plex DB-EX.

The page only draws (ADR-0017): it never reaches Plex, the encoder or a tokenizer.
Everything it shows comes through here.

- **Searching and drawing** run `plexdb-read search`, the reader crate's one
  implementation (`plexdb.shot_search`), so the page, `plexdb search` and a Rust
  consumer cannot disagree.
- **Token boxes** come from the Hugging Face tokenizer matching `PLEXDB_FRAME_MODEL`.
  The same tokenizer cuts a phrase at the 75th token before it is searched, so the
  page's "not searched" line is true.
- **Frames and clips** are cut by ffmpeg from the Plex part (`FfmpegMedia`) and kept
  in one least-recently-used cache under `<explore data>/clips`.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .errors import ConfigError, MediaError
from .frame_harvest import FfmpegMedia
from .frames import UnknownItem, open_frames, queue
from .shot_search import reader_binary, search_argv
from .store import open_readonly

#: CLIP reads at most this many tokens of a phrase (its context is 77, less the
#: start and end markers). A token past it never reaches the encoder.
TOKEN_LIMIT = 75
#: Item_ids in one pool, negatives in one search, and characters in one text.
MAX_POOL = 5000
MAX_NEGATIVES = 20
MAX_TEXT = 4000
MAX_ITEM_ID = 200
#: Rows a search returns, best first; every drawn row is added to them.
RESULT_ROWS = 100
#: Frames beside the title in a row.
THUMBS = 3
#: Shared least-recently-used ceiling of every cached frame and clip.
CACHE_BYTES = 2 << 30
#: Suggestions in the pool's add box.
SUGGESTIONS = 8
#: Reader runs and ffmpeg cuts at once. Plex DB-EX has no login, so these bound
#: what an anonymous caller can spend.
SEARCH_SLOTS = threading.BoundedSemaphore(2)
CUT_SLOTS = threading.BoundedSemaphore(3)
#: Seconds the reader may run: a new phrase waits on the encoder, up to its 300 s.
SEARCH_SECONDS = 330.0
#: How long a frame or clip waits for a free ffmpeg slot.
CUT_WAIT_SECONDS = 120.0
FRAME_HEIGHT = 224
LIGHTBOX_HEIGHT = 540
CLIP_WIDTH = 640
#: The longest window a clip may cut: a Shot is seconds, never minutes.
MAX_CLIP_MS = 120_000
#: Seconds a failed tokenizer load is remembered before the next attempt.
RETRY_SECONDS = 30.0
FORMULA = "score = phrase similarity − weight × strongest negative similarity"


class SearchUnavailable(Exception):
    """Something the tab needs is missing or unreachable; the message says what."""


class EncoderDown(SearchUnavailable):
    """The reader could not reach the encoder to embed these phrases."""

    def __init__(self, url: str, phrases: list[str], reason: str) -> None:
        super().__init__(f"the encoder at {url} refused {len(phrases)} phrase(s): {reason}")
        self.url = url
        self.phrases = phrases
        self.reason = reason


class NoFrame(LookupError):
    """No frame or clip can be cut for this request."""


# --- tokens --------------------------------------------------------------------


class PhraseTokenizer:
    """The tokenizer for `PLEXDB_FRAME_MODEL`, loaded on first use.

    Loaded from `explicit` when that file is set; otherwise from a copy of the
    model's `tokenizer.json` kept in `cache_dir`; otherwise fetched once from the
    Hugging Face hub by model name and saved there. `fetch` is the seam a test
    replaces.
    """

    def __init__(
        self,
        model: str,
        cache_dir: Path,
        explicit: Path | None = None,
        fetch: Callable[[str], Any] | None = None,
    ) -> None:
        self.model = model
        self._cache_dir = cache_dir
        self._explicit = explicit
        self._fetch = fetch
        self._lock = threading.Lock()
        self._tokenizer: Any = None
        self._failure: tuple[float, SearchUnavailable] | None = None

    def _cache_file(self) -> Path:
        return self._cache_dir / ("tokenizer-" + self.model.replace("/", "--") + ".json")

    def status(self) -> str | None:
        """Why the tokenizer cannot be used, or None once it can."""
        try:
            self._load()
        except SearchUnavailable as err:
            return str(err)
        return None

    def _load(self) -> Any:
        with self._lock:
            if self._tokenizer is not None:
                return self._tokenizer
            # A failed fetch is not retried on every keystroke of every client.
            if self._failure and time.monotonic() - self._failure[0] < RETRY_SECONDS:
                raise self._failure[1]
            from tokenizers import Tokenizer

            try:
                if self._explicit is not None:
                    self._tokenizer = Tokenizer.from_file(str(self._explicit))
                elif self._cache_file().exists():
                    self._tokenizer = Tokenizer.from_file(str(self._cache_file()))
                else:
                    if not self.model:
                        raise SearchUnavailable("PLEXDB_FRAME_MODEL is not set")
                    fetch = self._fetch or Tokenizer.from_pretrained
                    self._tokenizer = fetch(self.model)
                    try:
                        self._cache_dir.mkdir(parents=True, exist_ok=True)
                        self._tokenizer.save(str(self._cache_file()))
                    except OSError:
                        pass  # the next process fetches again; this one has it
            except SearchUnavailable as err:
                self._failure = (time.monotonic(), err)
                raise
            except Exception as err:
                where = self._explicit or self._cache_file()
                failure = SearchUnavailable(
                    f"no tokenizer for {self.model or 'the encoder model'}: {err}. "
                    f"Set PLEXDB_FRAME_TOKENIZER to its tokenizer.json (looked in {where})"
                )
                self._failure = (time.monotonic(), failure)
                raise failure from err
            return self._tokenizer

    def tokens(self, text: str) -> list[tuple[str, int, int]]:
        """Each token as (text, start, end), offsets in characters of `text`."""
        found = self._load().encode(text, add_special_tokens=False)
        return [(text[start:end], start, end) for start, end in found.offsets]

    def analyse(self, text: str) -> dict[str, object]:
        pieces = self.tokens(text)
        return {
            "count": len(pieces),
            "limit": TOKEN_LIMIT,
            "tokens": [
                {"text": piece, "start": start, "end": end, "over": i >= TOKEN_LIMIT}
                for i, (piece, start, end) in enumerate(pieces)
            ],
        }

    def searched(self, text: str) -> str:
        """`text` up to the end of its 75th token: the part the encoder reads."""
        pieces = self.tokens(text)
        return text if len(pieces) <= TOKEN_LIMIT else text[: pieces[TOKEN_LIMIT - 1][2]]


# --- the media cache -------------------------------------------------------------


class MediaCache:
    """Cut frames and clips on disk, evicting the least recently used past `cap`."""

    def __init__(self, root: Path, cap: int = CACHE_BYTES) -> None:
        self.root = root
        self._cap = cap
        self._lock = threading.Lock()
        self._keys: dict[str, threading.Lock] = {}

    def path(self, name: str) -> Path:
        return self.root / name

    def get(self, name: str, make: Callable[[], bytes]) -> Path:
        """The cached file for `name`, made by `make` once however many ask."""
        target = self.path(name)
        with self._lock:
            gate = self._keys.setdefault(name, threading.Lock())
        try:
            with gate:
                try:
                    os.utime(target)  # a hit; raises when absent or just evicted
                    return target
                except FileNotFoundError:
                    pass
                data = make()
                target.parent.mkdir(parents=True, exist_ok=True)
                partial = target.with_name(target.name + ".part")
                partial.write_bytes(data)
                partial.replace(target)
                self._evict()
            return target
        finally:
            with self._lock:
                self._keys.pop(name, None)

    def _evict(self) -> None:
        with self._lock:
            files = [p for p in self.root.rglob("*") if p.is_file() and p.suffix != ".part"]
            stats = {p: p.stat() for p in files}
            total = sum(s.st_size for s in stats.values())
            for victim in sorted(files, key=lambda p: stats[p].st_mtime):
                if total <= self._cap:
                    break
                total -= stats[victim].st_size
                victim.unlink(missing_ok=True)


# --- the tab -----------------------------------------------------------------------


@dataclass(frozen=True)
class PoolTitle:
    item_id: str
    title: str
    year: int | None
    kind: str
    state: str  # "indexed" | "failed" | "queued" | "none"


def _ms(value: int) -> str:
    seconds, milli = divmod(max(0, value), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{milli // 100}"


class SearchTab:
    """Everything `/api/search/*` answers with."""

    def __init__(
        self,
        config: Config,
        store_path: Path,
        data_dir: Path,
        *,
        tokenizer: PhraseTokenizer | None = None,
        media: Callable[[], FfmpegMedia] | None = None,
        cache_cap: int = CACHE_BYTES,
    ) -> None:
        self._config = config
        self._store = store_path
        self.tokenizer = tokenizer or PhraseTokenizer(
            config.frame_model, data_dir, config.frame_tokenizer
        )
        self._media_factory = media
        self.cache = MediaCache(data_dir / "clips", cache_cap)

    # -- configuration, shown to the person ------------------------------------------

    def _frames_file(self) -> Path:
        path = self._config.frames_path
        if path is None:
            raise SearchUnavailable(
                "PLEXDB_FRAMES_DIR is not set, so there is no Frame index to search"
            )
        if not path.exists():
            raise SearchUnavailable(f"no Frame index at {path} yet — nothing has been indexed")
        return path

    def _frames_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self._frames_file()}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def status(self) -> dict[str, object]:
        """What the tab needs, found or missing, and the counts the empty page shows."""
        cfg = self._config
        problems: dict[str, str] = {}
        try:
            reader_binary(cfg)
        except ConfigError as err:
            problems["reader"] = str(err)
        if not cfg.frame_encoder_url:
            problems["encoder"] = "PLEXDB_FRAME_ENCODER_URL is not set"
        tokenizer = self.tokenizer.status()
        if tokenizer is not None:
            problems["tokenizer"] = tokenizer
        media = self._media_problem()
        if media:
            problems["media"] = media
        movies = indexed = 0
        try:
            with closing(self._frames_conn()) as frames:
                indexed = int(
                    frames.execute(
                        "SELECT count(*) FROM frame_index_state WHERE error IS NULL"
                    ).fetchone()[0]
                )
        except SearchUnavailable as err:
            problems["frames"] = str(err)
        except sqlite3.DatabaseError as err:
            problems["frames"] = f"cannot read the Frame index: {err}"
        with open_readonly(self._store) as conn:
            movies = int(
                conn.execute("SELECT count(*) FROM items WHERE type = 'movie'").fetchone()[0]
            )
        return {
            "ready": not problems,
            "problems": problems,
            "model": cfg.frame_model,
            "encoder_url": cfg.frame_encoder_url,
            "movies": movies,
            "indexed": indexed,
            "limit": TOKEN_LIMIT,
        }

    def _media_problem(self) -> str | None:
        if self._media_factory is not None:
            return None
        import shutil

        missing = [
            name for name in (self._config.ffmpeg, self._config.ffprobe) if not shutil.which(name)
        ]
        if missing:
            return f"{' and '.join(missing)} not found (PLEXDB_FFMPEG, PLEXDB_FFPROBE)"
        if not self._config.plex_url or not self._config.plex_token:
            return "PLEX_URL and PLEX_TOKEN are not set, so frames and clips cannot be cut"
        return None

    # -- tokens ------------------------------------------------------------------------

    def tokenize(self, texts: object) -> dict[str, object]:
        if not isinstance(texts, list) or len(texts) > MAX_NEGATIVES + 1:
            raise ValueError(f"texts must be a list of at most {MAX_NEGATIVES + 1} strings")
        out = []
        for text in texts:
            if not isinstance(text, str) or len(text) > MAX_TEXT:
                raise ValueError(f"each text must be a string of at most {MAX_TEXT} characters")
            out.append(self.tokenizer.analyse(text))
        return {"fields": out, "limit": TOKEN_LIMIT, "model": self.tokenizer.model}

    # -- the pool ------------------------------------------------------------------------

    def suggest(self, q: str) -> dict[str, object]:
        """Titles and collections whose name contains `q`, for the pool's add box."""
        q = q.strip()
        if len(q) < 2:
            raise ValueError("q must be at least 2 characters")
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        low = q.lower()
        with open_readonly(self._store) as conn:
            titles = conn.execute(
                "SELECT item_id, title, year, type FROM items "
                "WHERE type IN ('movie', 'show') AND title LIKE ? ESCAPE '\\'",
                (like,),
            ).fetchall()
            lists = conn.execute(
                "SELECT c.collection_id, c.name, count(m.item_id) FROM collection c "
                "JOIN collection_membership m USING (collection_id) "
                "WHERE c.name LIKE ? ESCAPE '\\' GROUP BY c.collection_id",
                (like,),
            ).fetchall()
        titles.sort(
            key=lambda r: (
                not (r[1] or "").lower().startswith(low),
                (r[1] or "").lower(),
                r[2] or 0,
            )
        )
        lists.sort(key=lambda r: (not (r[1] or "").lower().startswith(low), (r[1] or "").lower()))
        return {
            "titles": [
                {"item_id": r[0], "title": r[1], "year": r[2], "kind": r[3]}
                for r in titles[:SUGGESTIONS]
            ],
            "collections": [
                {"collection_id": r[0], "name": r[1], "members": r[2]} for r in lists[:SUGGESTIONS]
            ],
        }

    def collection(self, collection_id: str) -> list[str]:
        with open_readonly(self._store) as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM collection WHERE collection_id = ?", (collection_id,)
                ).fetchone()
                is None
            ):
                raise NoFrame(f"no collection {collection_id!r}")
            rows = conn.execute(
                "SELECT m.item_id FROM collection_membership m "
                "JOIN items i USING (item_id) WHERE m.collection_id = ? "
                "ORDER BY m.rank IS NULL, m.rank, i.title",
                (collection_id,),
            ).fetchall()
        return [r[0] for r in rows[:MAX_POOL]]

    def _ids(self, raw: object) -> list[str]:
        if not isinstance(raw, list) or not raw:
            raise ValueError("item_ids must be a non-empty list")
        if len(raw) > MAX_POOL:
            raise ValueError(f"a pool holds at most {MAX_POOL} titles")
        ids: list[str] = []
        for item in raw:
            if not isinstance(item, str) or not item or len(item) > MAX_ITEM_ID:
                raise ValueError(f"each item_id must be a string of 1-{MAX_ITEM_ID} characters")
            if item not in ids:
                ids.append(item)
        return ids

    def pool(self, raw: object) -> dict[str, object]:
        """Each pool title with whether the Frame index holds it."""
        ids = self._ids(raw)
        states = self._states(ids)
        titles = self._titles(ids)
        found = [
            PoolTitle(i, titles[i][0], titles[i][1], titles[i][2], states.get(i, "none"))
            for i in ids
            if i in titles
        ]
        return {
            "titles": [t.__dict__ for t in found],
            "unknown": [i for i in ids if i not in titles],
            "indexed": sum(t.state == "indexed" for t in found),
        }

    def _titles(self, ids: list[str]) -> dict[str, tuple[str, int | None, str]]:
        out: dict[str, tuple[str, int | None, str]] = {}
        with open_readonly(self._store) as conn:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                marks = ",".join("?" * len(chunk))
                for r in conn.execute(
                    f"SELECT item_id, title, year, type FROM items WHERE item_id IN ({marks})",
                    chunk,
                ):
                    out[r[0]] = (r[1], r[2], r[3])
        return out

    def _states(self, ids: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            conn = self._frames_conn()
        except SearchUnavailable:
            return out
        with closing(conn):
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                marks = ",".join("?" * len(chunk))
                for r in conn.execute(
                    f"SELECT item_id FROM frame_queue WHERE item_id IN ({marks})", chunk
                ):
                    out[r[0]] = "queued"
                for r in conn.execute(
                    f"SELECT item_id, error FROM frame_index_state WHERE item_id IN ({marks})",
                    chunk,
                ):
                    out[r[0]] = "failed" if r[1] else "indexed"
        return out

    def queue_missing(self, raw: object) -> dict[str, object]:
        """Queue every pool title the Frame index does not hold, as `plexdb frames queue`."""
        ids = self._ids(raw)
        states = self._states(ids)
        wanted = [i for i in ids if states.get(i, "none") in ("none", "failed")]
        known = self._titles(wanted)
        added = 0
        with open_frames(self._frames_file()) as frames:
            for item_id in wanted:
                try:
                    if queue(
                        frames,
                        item_id,
                        known=item_id in known,
                        by="explore",
                        now=datetime.now().astimezone(),
                    ):
                        added += 1
                except UnknownItem:
                    continue
            waiting = int(frames.execute("SELECT count(*) FROM frame_queue").fetchone()[0])
        return {"queued": added, "waiting": waiting}

    # -- searching -----------------------------------------------------------------------

    def _run_reader(self, argv: list[str], pool: list[str], phrases: list[str]) -> dict[str, Any]:
        if not SEARCH_SLOTS.acquire(blocking=False):
            raise SearchUnavailable("two searches are already running; try again in a moment")
        try:
            done = subprocess.run(  # noqa: S603
                argv,
                input="\n".join(pool),
                capture_output=True,
                text=True,
                check=False,
                timeout=SEARCH_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise SearchUnavailable(f"plexdb-read ran past {SEARCH_SECONDS:.0f} s") from None
        finally:
            SEARCH_SLOTS.release()
        if done.returncode != 0:
            lines = [line for line in done.stderr.splitlines() if line.strip()]
            message = (lines[0].strip() if lines else f"plexdb-read exited {done.returncode}")[:300]
            if "encoder" in message.lower():
                reason = re.sub(r"^(error: )?encoder \w+( at \S+?)?: ", "", message)
                raise EncoderDown(self._config.frame_encoder_url, phrases, reason)
            raise SearchUnavailable(message)
        try:
            parsed = json.loads(done.stdout)
        except ValueError:
            raise SearchUnavailable("plexdb-read printed something that is not JSON") from None
        if not isinstance(parsed, dict):
            raise SearchUnavailable("plexdb-read printed something that is not an object")
        return parsed

    def search(self, body: dict[str, object]) -> dict[str, object]:
        phrase = body.get("phrase")
        if not isinstance(phrase, str) or not phrase.strip():
            raise ValueError("phrase must be text")
        if len(phrase) > MAX_TEXT:
            raise ValueError(f"phrase is over {MAX_TEXT} characters")
        raw_negatives = body.get("negatives", [])
        if not isinstance(raw_negatives, list) or len(raw_negatives) > MAX_NEGATIVES:
            raise ValueError(f"negatives must be a list of at most {MAX_NEGATIVES} strings")
        if not all(isinstance(n, str) and len(n) <= MAX_TEXT for n in raw_negatives):
            raise ValueError("each negative must be text")
        weight = _number(body.get("weight"), "weight", low=0.0, high=10.0)
        temperature = _number(body.get("temperature"), "temperature", low=0.0, high=100.0)
        if temperature == 0.0:
            raise ValueError("temperature must be above 0")
        draw = body.get("draw")
        n = seed = None
        if draw is not None:
            if not isinstance(draw, dict):
                raise ValueError("draw must be an object with n and seed")
            n = _whole(draw.get("n"), "draw.n", low=1, high=RESULT_ROWS)
            seed = _whole(draw.get("seed", 0), "draw.seed", low=0, high=2**63 - 1)
        ids = self._ids(body.get("pool"))
        self._frames_file()
        # What the encoder reads: each text cut at its 75th token, blanks dropped.
        searched = self.tokenizer.searched(phrase.strip())
        negatives = [
            cut for text in raw_negatives if (cut := self.tokenizer.searched(str(text).strip()))
        ]
        phrases = [searched, *negatives]

        def argv(draw: int | None = None, seed: int | None = None) -> list[str]:
            return search_argv(
                self._config,
                searched,
                snapshot=self._store,
                negatives=negatives,
                weight=weight,
                temperature=temperature,
                draw=draw,
                seed=seed,
            )

        found = self._run_reader(argv(), ids, phrases)
        picks: dict[tuple[str, int], int] = {}
        if n is not None:
            drawn = self._run_reader(argv(n, seed), ids, phrases)
            for order, shot in enumerate(drawn.get("shots", []), start=1):
                picks[(shot["item_id"], shot["in_ms"])] = order
        return self._shape(found, ids, searched, negatives, weight, temperature, n, seed, picks)

    def _shape(
        self,
        found: dict[str, Any],
        ids: list[str],
        searched: str,
        negatives: list[str],
        weight: float | None,
        temperature: float | None,
        n: int | None,
        seed: int | None,
        picks: dict[tuple[str, int], int],
    ) -> dict[str, object]:
        shots = list(found.get("shots", []))
        keep = [
            (rank, shot)
            for rank, shot in enumerate(shots, start=1)
            if rank <= RESULT_ROWS or (shot["item_id"], shot["in_ms"]) in picks
        ]
        titles = self._titles(sorted({shot["item_id"] for _, shot in keep}))
        rows = []
        for rank, shot in keep:
            sims = shot.get("negative_sims", [])
            worst = max(range(len(sims)), key=sims.__getitem__) if sims else None
            frames = [{"t_ms": t, "sim": sim} for t, sim in shot["frames"]]
            title, year, kind = titles.get(shot["item_id"], (shot["item_id"], None, ""))
            rows.append(
                {
                    "rank": rank,
                    "item_id": shot["item_id"],
                    "title": title,
                    "year": year,
                    "kind": kind,
                    "in_ms": shot["in_ms"],
                    "out_ms": shot["out_ms"],
                    "in": _ms(shot["in_ms"]),
                    "out": _ms(shot["out_ms"]),
                    "seconds": round((shot["out_ms"] - shot["in_ms"]) / 1000, 1),
                    "frames": frames,
                    "thumbs": _spread(frames, THUMBS),
                    "phrase_sim": shot["phrase_sim"],
                    "score": shot["score"],
                    "p": shot.get("p"),
                    "strongest_negative": (
                        None if worst is None else {"phrase": negatives[worst], "sim": sims[worst]}
                    ),
                    "drawn": picks.get((shot["item_id"], shot["in_ms"])),
                }
            )
        return {
            "shots": rows,
            "total": len(shots),
            "indexed": found.get("indexed", 0),
            "pool": found.get("pool", len(ids)),
            "model": found.get("model", ""),
            "searched": {"phrase": searched, "negatives": negatives},
            "weight": 0.5 if weight is None else weight,
            "temperature": temperature,
            "draw": None if n is None else {"n": n, "seed": seed, "picks": len(picks)},
            "formula": FORMULA,
        }

    # -- frames and clips -----------------------------------------------------------------

    def _part(self, item_id: str) -> tuple[int, int | None]:
        """The Plex part the title was indexed from and its length in ms, if known."""
        try:
            with closing(self._frames_conn()) as conn:
                row = conn.execute(
                    "SELECT part_id, duration_ms FROM frame_index_state "
                    "WHERE item_id = ? AND model IS NOT NULL",
                    (item_id,),
                ).fetchone()
        except SearchUnavailable as err:
            raise NoFrame(str(err)) from None
        if row is None or row[0] is None:
            raise NoFrame(f"{item_id} is not in the Frame index")
        return int(row[0]), None if row[1] is None else int(row[1])

    def _media(self) -> FfmpegMedia:
        if self._media_factory is not None:
            return self._media_factory()
        problem = self._media_problem()
        if problem:
            raise SearchUnavailable(problem)
        cfg = self._config
        return FfmpegMedia(cfg.plex_url, cfg.plex_token, ffmpeg=cfg.ffmpeg, ffprobe=cfg.ffprobe)

    def _cut(self, make: Callable[[], bytes]) -> Callable[[], bytes]:
        def run() -> bytes:
            # A results page asks for thirty thumbnails at once; they take turns rather than fail.
            if not CUT_SLOTS.acquire(timeout=CUT_WAIT_SECONDS):
                raise SearchUnavailable("the cuts ahead of this one took too long; try again")
            try:
                return make()
            except MediaError as err:
                raise SearchUnavailable(str(err)) from None
            finally:
                CUT_SLOTS.release()

        return run

    def frame(self, item_id: str, t_ms: int, *, large: bool = False) -> Path:
        part, length = self._part(item_id)
        if length is not None and t_ms > length:
            raise ValueError(f"t_ms is past the end of the title ({length} ms)")
        height = LIGHTBOX_HEIGHT if large else FRAME_HEIGHT

        def make() -> bytes:
            (jpeg,) = self._media().frames(part, [t_ms], height=height)
            if jpeg is None:
                raise MediaError(f"part {part}: no frame at {t_ms} ms")
            return jpeg

        return self.cache.get(
            f"frames/{_slug(item_id)}-{part}-{t_ms}-{height}.jpg", self._cut(make)
        )

    def clip(self, item_id: str, in_ms: int, out_ms: int) -> Path:
        part, length = self._part(item_id)
        if not 0 <= in_ms < out_ms:
            raise ValueError("the clip window is empty")
        if out_ms - in_ms > MAX_CLIP_MS or (length is not None and out_ms > length):
            raise ValueError(
                f"the clip window must lie inside the title and be at most {MAX_CLIP_MS} ms"
            )
        return self.cache.get(
            f"{_slug(item_id)}-{part}-{in_ms}-{out_ms}-{CLIP_WIDTH}.mp4",
            self._cut(lambda: self._media().clip(part, in_ms, out_ms, width=CLIP_WIDTH)),
        )


def _slug(item_id: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in item_id)


def _spread(frames: list[dict[str, Any]], count: int) -> list[int]:
    """`count` offsets spread evenly across a Shot's frames, first to last."""
    if len(frames) <= count:
        return [f["t_ms"] for f in frames]
    step = (len(frames) - 1) / (count - 1)
    return [frames[round(i * step)]["t_ms"] for i in range(count)]


def _number(value: object, name: str, *, low: float, high: float) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a number")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return float(value)


def _whole(value: object, name: str, *, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a whole number")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


__all__ = [
    "TOKEN_LIMIT",
    "EncoderDown",
    "MediaCache",
    "NoFrame",
    "PhraseTokenizer",
    "SearchTab",
    "SearchUnavailable",
]
