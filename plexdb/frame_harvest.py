"""The frame harvester: reads each title through Plex and fills the Frame index.

For one title it finds the Cuts (scdet's scene changes and the file's chapter
marks), picks the frames to sample, embeds them through the encoder client
(ADR-0025) and replaces the title's rows in `frames.db` in one transaction. The
video is read over HTTP from `{PLEX_URL}/library/parts/<part_id>/file`, so the
harvester needs no media mount. It writes `frames.db` alone; the store is read
through a separate read-only connection (ADR-0024).

**What it works on, in order.** (1) Titles someone queued, oldest first.
(2) Titles never indexed, movies before episodes, newest Plex `addedAt` first.
(3) Titles whose `file_hash` changed since they were indexed, least recently
indexed first. A title that failed is retried by neither (2) nor (3) until its
`file_hash` changes or it is queued again.

**Sampling.** The boundaries are the Cuts, ignoring any closer than
`MIN_SHOT_MS` to the one before. Between two boundaries is a Shot. A Shot of
`SPAN_MS` or less gets one frame, at its midpoint; a longer Shot gets one frame
every `SPAN_MS`, starting `SPAN_MS / 2` after its first boundary.

**Stopping.** A run ends when the window closes (after the title in hand), when
nothing is left, at `--limit`, or when the encoder does not answer. An encoder
that stops answering leaves the title in hand untouched; the next run takes it
first because its rows are still absent or stale.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

import httpx

from . import frames
from .config import Config
from .errors import EncoderError, EncoderUnavailable, MediaError, PlexdbError
from .frame_encoder import EncoderClient
from .plex_client import PLEX_TYPE_EPISODE, PLEX_TYPE_MOVIE, PlexSource
from .store import open_readonly

__all__ = [
    "BATCH",
    "MIN_SHOT_MS",
    "SPAN_MS",
    "Candidate",
    "Cut",
    "FfmpegMedia",
    "HarvestResult",
    "MediaReader",
    "harvest",
    "run_in_windows",
    "sample_times",
    "shots",
]

#: Boundaries closer together than this are one boundary for sampling (a dissolve
#: makes scdet fire on consecutive frames). Every Cut is still stored.
MIN_SHOT_MS = 500
#: A Shot this long or shorter gets one frame; a longer one a frame this often.
SPAN_MS = 2000
#: Frames per embeddings request.
BATCH = 32
#: Frames cut out per ffmpeg process.
_EXTRACT_CHUNK = 24
#: How much of the file the hash reads.
_HASH_BYTES = 1 << 20
#: Consecutive titles the encoder may reject (4xx other than 429) before the run
#: stops: one corrupt frame fails one title, a wrong model name fails them all.
_MAX_ENCODER_REJECTS = 3
_QUANT = "int8"
#: A stalled Plex read ends ffmpeg after this many microseconds without data.
_IO_TIMEOUT_US = "60000000"
#: The longest one ffmpeg or ffprobe process may run: a full decode for scdet, minutes
#: for anything else.
_DECODE_TIMEOUT_S = 6 * 3600
_PROBE_TIMEOUT_S = 900

_STOP_WINDOW = "window closed"
_STOP_EMPTY = "nothing to do"
_STOP_LIMIT = "limit reached"
_STOP_ENCODER = "encoder unavailable"
_STOP_REJECTED = "encoder rejected consecutive titles"


@dataclass(frozen=True)
class Cut:
    t_ms: int
    score: float | None
    source: str


@dataclass(frozen=True)
class Candidate:
    """One title the harvester may index, as Plex describes it now."""

    item_id: str
    part_id: int
    size: int
    duration_ms: int
    reason: str
    #: Already computed by the discovery of a changed title, so it is not read twice.
    file_hash: str | None = None


class MediaReader(Protocol):
    """The three things the harvester asks of a video file. `FfmpegMedia` is the
    real one; a test substitutes a fake that decodes nothing."""

    def file_hash(self, part_id: int, size: int) -> str: ...

    def cuts(self, part_id: int) -> list[Cut]: ...

    def frames(self, part_id: int, times_ms: Sequence[int]) -> list[bytes | None]:
        """One JPEG per time, `None` where the file has no frame there."""
        ...


# --- sampling -----------------------------------------------------------------


def shots(cut_times_ms: Sequence[int], duration_ms: int) -> list[tuple[int, int]]:
    """The `(start, end)` of each Shot between the boundaries `cut_times_ms`."""
    bounds = [0]
    for t_ms in sorted(set(cut_times_ms)):
        if MIN_SHOT_MS <= t_ms - bounds[-1] and t_ms < duration_ms:
            bounds.append(t_ms)
    bounds.append(duration_ms)
    return list(zip(bounds, bounds[1:], strict=False))


def sample_times(shot_list: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """`(t_ms, shot_ix)` for every frame to embed."""
    out: list[tuple[int, int]] = []
    for ix, (start, end) in enumerate(shot_list):
        if end - start <= SPAN_MS:
            out.append(((start + end) // 2, ix))
            continue
        t_ms = start + SPAN_MS // 2
        while t_ms < end:
            out.append((t_ms, ix))
            t_ms += SPAN_MS
    return out


# --- reading through Plex -----------------------------------------------------


class FfmpegMedia:
    """Reads a Plex part with ffmpeg and ffprobe over HTTP. The token travels in a
    header, never in the URL, so it appears in no log line."""

    def __init__(
        self,
        plex_url: str,
        token: str,
        *,
        ffmpeg: str,
        ffprobe: str,
        http: httpx.Client | None = None,
    ) -> None:
        self._base = plex_url.rstrip("/")
        self._token = token
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._http = http or httpx.Client(timeout=60.0)

    def _url(self, part_id: int) -> str:
        return f"{self._base}/library/parts/{part_id}/file"

    def _input(self, part_id: int) -> list[str]:
        return [
            "-rw_timeout", _IO_TIMEOUT_US,
            "-headers", f"X-Plex-Token: {self._token}\r\n",
            "-i", self._url(part_id),
        ]  # fmt: skip

    def file_hash(self, part_id: int, size: int) -> str:
        """sha256 over the part's size, its `Last-Modified`, and its first MiB."""
        headers = {"X-Plex-Token": self._token, "Range": f"bytes=0-{_HASH_BYTES - 1}"}
        try:
            with self._http.stream("GET", self._url(part_id), headers=headers) as resp:
                if resp.status_code not in (200, 206):
                    raise MediaError(f"part {part_id}: Plex answered {resp.status_code}")
                modified = resp.headers.get("Last-Modified", "")
                head = bytearray()
                for chunk in resp.iter_bytes():
                    head += chunk
                    if len(head) >= _HASH_BYTES:
                        break
        except httpx.HTTPError as err:
            raise MediaError(
                f"part {part_id}: cannot read through Plex: {type(err).__name__}"
            ) from None
        digest = hashlib.sha256(f"{size}|{modified}|".encode())
        digest.update(bytes(head[:_HASH_BYTES]))
        return digest.hexdigest()

    def _run(
        self, argv: list[str], what: str, *, timeout_s: float = _PROBE_TIMEOUT_S
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(  # noqa: S603
                argv,
                capture_output=True,
                text=True,
                check=False,
                stdin=subprocess.DEVNULL,
                timeout=timeout_s,
            )
        except FileNotFoundError:
            raise MediaError(f"{what}: {argv[0]} is not installed") from None
        except subprocess.TimeoutExpired:
            raise MediaError(f"{what}: {argv[0]} ran past {timeout_s:.0f} s") from None

    def cuts(self, part_id: int) -> list[Cut]:
        found = self._scdet(part_id) + self._chapters(part_id)
        return sorted(found, key=lambda cut: (cut.t_ms, cut.source))

    def _scdet(self, part_id: int) -> list[Cut]:
        argv = [
            self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            *self._input(part_id),
            "-an", "-sn", "-dn",
            "-vf", "scale=-2:224,scdet=t=10:s=1,metadata=print:file=-",
            "-f", "null", "-",
        ]  # fmt: skip
        done = self._run(argv, f"part {part_id}: scdet", timeout_s=_DECODE_TIMEOUT_S)
        if done.returncode != 0:
            raise MediaError(
                f"part {part_id}: ffmpeg scdet exited {done.returncode}: "
                f"{done.stderr.strip()[-200:]}"
            )
        return parse_scdet(done.stdout)

    def _chapters(self, part_id: int) -> list[Cut]:
        argv = [
            self._ffprobe, "-v", "error", "-headers", f"X-Plex-Token: {self._token}\r\n",
            "-show_chapters", "-print_format", "json", self._url(part_id),
        ]  # fmt: skip
        done = self._run(argv, f"part {part_id}: chapters")
        if done.returncode != 0:
            raise MediaError(
                f"part {part_id}: ffprobe exited {done.returncode}: {done.stderr.strip()[-200:]}"
            )
        return parse_chapters(done.stdout)

    def frames(self, part_id: int, times_ms: Sequence[int]) -> list[bytes | None]:
        out: list[bytes | None] = []
        for start in range(0, len(times_ms), _EXTRACT_CHUNK):
            chunk = times_ms[start : start + _EXTRACT_CHUNK]
            with tempfile.TemporaryDirectory(prefix="plexdb-frames-") as tmp:
                argv = [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
                for t_ms in chunk:
                    argv += ["-ss", f"{t_ms / 1000:.3f}", *self._input(part_id)]
                for i in range(len(chunk)):
                    argv += [
                        "-map", f"{i}:v:0", "-frames:v", "1", "-vf", "scale=-2:224",
                        "-q:v", "3", "-y", str(Path(tmp) / f"{i}.jpg"),
                    ]  # fmt: skip
                done = self._run(argv, f"part {part_id}: frames")
                got = [Path(tmp) / f"{i}.jpg" for i in range(len(chunk))]
                if done.returncode != 0 and not any(path.exists() for path in got):
                    raise MediaError(
                        f"part {part_id}: ffmpeg exited {done.returncode}: "
                        f"{done.stderr.strip()[-200:]}"
                    )
                out += [path.read_bytes() if path.exists() else None for path in got]
        return out


def parse_scdet(text: str) -> list[Cut]:
    """Cuts from `metadata=print` output: a `lavfi.scd.score` and a `lavfi.scd.time`
    line per detected scene change."""
    cuts: list[Cut] = []
    score: float | None = None
    for line in text.splitlines():
        if line.startswith("lavfi.scd.score="):
            try:
                score = float(line.split("=", 1)[1])
            except ValueError:
                raise MediaError(f"scdet printed an unreadable score: {line!r}") from None
        elif line.startswith("lavfi.scd.time="):
            try:
                t_ms = round(float(line.split("=", 1)[1]) * 1000)
            except ValueError:
                raise MediaError(f"scdet printed an unreadable time: {line!r}") from None
            if t_ms > 0:
                cuts.append(Cut(t_ms, score, "scdet"))
    return cuts


def parse_chapters(text: str) -> list[Cut]:
    try:
        body = json.loads(text or "{}")
        marks = [round(float(ch["start_time"]) * 1000) for ch in body.get("chapters", [])]
    except (ValueError, KeyError, TypeError):
        raise MediaError("ffprobe returned chapters that are not JSON") from None
    return [Cut(t_ms, None, "chapter") for t_ms in marks if t_ms > 0]


# --- discovery ------------------------------------------------------------------


def _plex_parts(
    store_conn: sqlite3.Connection, plex: PlexSource
) -> dict[str, tuple[str, int, int, int, int]]:
    """`item_id -> (kind, added_at, part_id, size, duration_ms)` for every movie and
    episode Plex lists with a media part. A title with several Plex copies takes the
    newest."""
    wanted: dict[str, tuple[str, str, str]] = {}
    for row in store_conn.execute(
        "SELECT p.rating_key, p.item_id, p.section_id, i.type FROM plex_items p "
        "JOIN items i ON i.item_id = p.item_id WHERE i.type IN ('movie', 'episode')"
    ):
        wanted[str(row[0])] = (str(row[1]), str(row[2]), str(row[3]))
    section_types = {section.key: section.type for section in plex.sections()}
    best: dict[str, tuple[str, int, int, int, int]] = {}
    for key in sorted({section for _, section, _ in wanted.values()}):
        kind = section_types.get(key)
        if kind not in ("movie", "show"):
            continue
        records = plex.items(key, PLEX_TYPE_MOVIE if kind == "movie" else PLEX_TYPE_EPISODE)
        for record in records:
            hit = wanted.get(str(record.get("ratingKey")))
            parts = [part for media in record.get("Media", []) for part in media.get("Part", [])]
            if hit is None or not parts:
                continue
            item_id, _, item_type = hit
            media_duration = record.get("Media", [{}])[0].get("duration") or parts[0].get(
                "duration"
            )
            entry = (
                item_type,
                int(record.get("addedAt") or 0),
                int(parts[0]["id"]),
                int(parts[0].get("size") or 0),
                int(media_duration or 0),
            )
            if item_id not in best or entry[1] > best[item_id][1]:
                best[item_id] = entry
    return best


def _candidates(
    frames_conn: sqlite3.Connection,
    parts: dict[str, tuple[str, int, int, int, int]],
    media: MediaReader,
    log: Callable[[str], None],
    keep_going: Callable[[], bool],
) -> Iterator[Candidate]:
    """Every title to index, in order, queued first. Read lazily: a queue row added
    during a run is seen before the backlog resumes."""

    def candidate(item_id: str, reason: str, file_hash: str | None = None) -> Candidate:
        _, _, part_id, size, duration_ms = parts[item_id]
        return Candidate(item_id, part_id, size, duration_ms, reason, file_hash)

    seen: set[str] = set()
    while True:
        row = frames_conn.execute(
            "SELECT item_id FROM frame_queue ORDER BY queued_at, item_id"
        ).fetchall()
        for r in row:
            if str(r[0]) not in parts and str(r[0]) not in seen:
                seen.add(str(r[0]))
                log(f"queued {r[0]} is not a movie or episode Plex lists with a media part")
        queued = [str(r[0]) for r in row if str(r[0]) in parts and str(r[0]) not in seen]
        if not queued:
            break
        seen.add(queued[0])
        yield candidate(queued[0], "queued")

    state = {
        str(r["item_id"]): r
        for r in frames_conn.execute("SELECT item_id, file_hash, indexed_at FROM frame_index_state")
    }
    fresh = sorted(
        (item_id for item_id in parts if item_id not in state and item_id not in seen),
        key=lambda item_id: (parts[item_id][0] != "movie", -parts[item_id][1], item_id),
    )
    for item_id in fresh:
        yield candidate(item_id, "new")

    for item_id in sorted(state, key=lambda item: (state[item]["indexed_at"], item)):
        if item_id not in parts or item_id in seen:
            continue
        if not keep_going():
            return
        try:
            current = media.file_hash(parts[item_id][2], parts[item_id][3])
        except MediaError as err:
            log(f"skip {item_id}: could not hash: {err}")
            continue
        if current != state[item_id]["file_hash"]:
            yield candidate(item_id, "changed", current)


# --- the run ----------------------------------------------------------------------


@dataclass
class HarvestResult:
    stopped: str = ""
    indexed: int = 0
    failed: int = 0
    titles: list[str] = field(default_factory=list)


def _vec_blob(vector: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _write_title(
    frames_conn: sqlite3.Connection,
    cand: Candidate,
    *,
    file_hash: str,
    cuts: Sequence[Cut],
    samples: Sequence[tuple[int, int]],
    vectors: Sequence[Sequence[float]],
    shot_count: int,
    model: str,
    now: datetime,
) -> None:
    with frames_conn:
        for table in ("frame_vec", "cuts"):
            frames_conn.execute(f"DELETE FROM {table} WHERE item_id = ?", (cand.item_id,))  # noqa: S608
        frames_conn.executemany(
            "INSERT OR IGNORE INTO cuts (item_id, t_ms, score, source) VALUES (?, ?, ?, ?)",
            [(cand.item_id, c.t_ms, c.score, c.source) for c in cuts],
        )
        frames_conn.executemany(
            "INSERT INTO frame_vec (item_id, t_ms, shot_ix, vec) "
            "VALUES (?, ?, ?, vec_quantize_int8(?, 'unit'))",
            [
                (cand.item_id, t_ms, ix, _vec_blob(vec))
                for (t_ms, ix), vec in zip(samples, vectors, strict=True)
            ],
        )
        frames_conn.execute(
            "INSERT OR REPLACE INTO frame_index_state (item_id, part_id, model, model_version, "
            "dims, quant, duration_ms, bytes, file_hash, shots, vectors, indexed_at, error) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                cand.item_id, cand.part_id, model, frames.FRAME_DIMS, _QUANT, cand.duration_ms,
                cand.size, file_hash, shot_count, len(samples), now.isoformat(),
            ),
        )  # fmt: skip
        frames_conn.execute("DELETE FROM frame_queue WHERE item_id = ?", (cand.item_id,))


def _record_failure(
    frames_conn: sqlite3.Connection,
    cand: Candidate,
    error: str,
    file_hash: str | None,
    now: datetime,
) -> None:
    """Keep whatever rows the title had; mark the attempt failed and take it off the queue."""
    with frames_conn:
        frames_conn.execute(
            "INSERT INTO frame_index_state (item_id, part_id, bytes, duration_ms, file_hash, "
            "indexed_at, error) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET part_id = excluded.part_id, "
            "bytes = excluded.bytes, file_hash = coalesce(excluded.file_hash, file_hash), "
            "indexed_at = excluded.indexed_at, error = excluded.error",
            (
                cand.item_id,
                cand.part_id,
                cand.size,
                cand.duration_ms,
                file_hash,
                now.isoformat(),
                error,
            ),
        )
        frames_conn.execute("DELETE FROM frame_queue WHERE item_id = ?", (cand.item_id,))


def _index_title(
    frames_conn: sqlite3.Connection,
    cand: Candidate,
    media: MediaReader,
    encoder: EncoderClient,
    now_fn: Callable[[], datetime],
    log: Callable[[str], None],
) -> str:
    """Index one title. Returns `"ok"`, `"failed"` (an unreadable file; recorded), or
    `"rejected"` (the encoder refused its frames; recorded). Raises `EncoderUnavailable`
    with nothing written when the encoder stops answering."""
    file_hash = cand.file_hash
    shot_count = vector_count = 0
    decode_s = encode_s = 0.0
    error: str | None = None
    outcome = "ok"
    try:
        if cand.duration_ms <= 0:
            raise MediaError(f"part {cand.part_id}: Plex reports no duration")
        file_hash = file_hash or media.file_hash(cand.part_id, cand.size)
        started = time.perf_counter()
        cuts = media.cuts(cand.part_id)
        shot_list = shots([c.t_ms for c in cuts], cand.duration_ms)
        samples = sample_times(shot_list)
        jpegs = media.frames(cand.part_id, [t_ms for t_ms, _ in samples])
        decode_s = time.perf_counter() - started
        kept = [(sample, jpeg) for sample, jpeg in zip(samples, jpegs, strict=True) if jpeg]
        if not kept:
            raise MediaError(f"part {cand.part_id}: no frame could be read")
        samples = [sample for sample, _ in kept]
        vectors: list[list[float]] = []
        model = encoder.model
        for start in range(0, len(kept), BATCH):
            answer = encoder.timed_images([jpeg for _, jpeg in kept[start : start + BATCH]])
            encode_s += answer.total_s
            model = answer.model or model
            vectors += answer.vectors
        if any(len(vec) != frames.FRAME_DIMS for vec in vectors):
            raise EncoderError(
                f"the encoder returned {len(vectors[0])}-wide vectors; "
                f"frame_vec holds {frames.FRAME_DIMS}"
            )
        shot_count, vector_count = len(shot_list), len(samples)
        _write_title(
            frames_conn, cand, file_hash=file_hash, cuts=cuts, samples=samples,
            vectors=vectors, shot_count=shot_count, model=model, now=now_fn(),
        )  # fmt: skip
    except EncoderUnavailable:
        raise
    except (MediaError, EncoderError) as err:
        error = str(err)
        outcome = "rejected" if isinstance(err, EncoderError) else "failed"
        _record_failure(frames_conn, cand, error, file_hash, now_fn())
    line = (
        f"{now_fn().isoformat(timespec='seconds')} item_id={cand.item_id} part={cand.part_id} "
        f"bytes={cand.size} decode_s={decode_s:.1f} encode_s={encode_s:.1f} "
        f"shots={shot_count} vectors={vector_count} reason={cand.reason}"
    )
    log(line if error is None else f"{line} error={error!r}")
    return outcome


def harvest(
    config: Config,
    *,
    plex: PlexSource,
    media: MediaReader,
    encoder: EncoderClient,
    now_fn: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ignore_hours: bool = False,
    limit: int | None = None,
    log: Callable[[str], None] = print,
) -> HarvestResult:
    """One harvest run over the configured Frame index."""
    path = frames.frames_path(config)
    window = frames.parse_hours(config.frame_hours)
    result = HarvestResult()
    with open_readonly(config.store_path) as store_conn:
        parts = _plex_parts(store_conn, plex)
    with frames.open_frames(path) as frames_conn:
        frames.set_indexing(frames_conn, None, now=now_fn())
        rejects = 0
        candidates = _candidates(
            frames_conn,
            parts,
            media,
            lambda msg: log(f"frames: {msg}"),
            lambda: ignore_hours or frames.inside_hours(window, now_fn()),
        )
        try:
            while True:
                if not ignore_hours and not frames.inside_hours(window, now_fn()):
                    result.stopped = _STOP_WINDOW
                    break
                if limit is not None and result.indexed + result.failed >= limit:
                    result.stopped = _STOP_LIMIT
                    break
                cand = next(candidates, None)
                if cand is None:
                    result.stopped = _STOP_EMPTY
                    break
                frames.set_indexing(frames_conn, cand.item_id, now=now_fn())
                try:
                    outcome = _index_title(
                        frames_conn,
                        cand,
                        media,
                        encoder,
                        now_fn,
                        lambda line: log(f"frames: {line}"),
                    )
                except EncoderUnavailable as err:
                    log(f"frames: stopping, {err}")
                    result.stopped = _STOP_ENCODER
                    break
                finally:
                    frames.set_indexing(frames_conn, None, now=now_fn())
                rejects = rejects + 1 if outcome == "rejected" else 0
                result.titles.append(cand.item_id)
                if outcome == "ok":
                    result.indexed += 1
                else:
                    result.failed += 1
                if rejects >= _MAX_ENCODER_REJECTS:
                    result.stopped = _STOP_REJECTED
                    break
        finally:
            frames.set_indexing(frames_conn, None, now=now_fn())
    return result


def run_in_windows(
    config: Config,
    run_once: Callable[[], str],
    *,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
    iterations: int | None = None,
    poll_s: float = 60.0,
    retry_s: float = 900.0,
) -> None:
    """Run `run_once` whenever `PLEXDB_FRAME_HOURS` is open. It returns why it
    stopped (the `harvest` stop reasons). After a run that emptied the work, the loop
    waits for the window to close; after an encoder that did not answer, it tries
    again in `retry_s`; a run that raised is logged and retried the same way."""
    window = frames.parse_hours(config.frame_hours)
    done = 0
    while iterations is None or done < iterations:
        done += 1
        if not frames.inside_hours(window, clock()):
            sleep(poll_s)
            continue
        try:
            stopped = run_once()
        except (PlexdbError, OSError, sqlite3.Error) as err:
            log(f"frames: harvest failed: {err}")
            sleep(retry_s)
            continue
        log(f"frames: harvest stopped: {stopped}")
        if stopped == _STOP_EMPTY:
            while frames.inside_hours(window, clock()):
                sleep(poll_s)
        elif stopped in (_STOP_ENCODER, _STOP_REJECTED):
            sleep(retry_s)


def configured_media(config: Config) -> FfmpegMedia:
    if shutil.which(config.ffmpeg) is None or shutil.which(config.ffprobe) is None:
        raise MediaError(
            f"{config.ffmpeg} and {config.ffprobe} must be installed "
            "(or named by PLEXDB_FFMPEG and PLEXDB_FFPROBE) to harvest frames"
        )
    return FfmpegMedia(
        config.plex_url, config.plex_token, ffmpeg=config.ffmpeg, ffprobe=config.ffprobe
    )
