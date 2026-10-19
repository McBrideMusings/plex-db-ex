"""The frame harvester: reads each title's file and fills the Frame index.

For one title it finds the Cuts (scdet's scene changes and the file's chapter
marks), picks the frames to sample, embeds them through the encoder client
(ADR-0025) and replaces the title's rows in `frames.db` in one transaction. Plex
says which part each title plays and where its file is; the video itself is read
from that file under `PLEXDB_MEDIA_DIR`, read-only. It writes `frames.db` alone;
the store is read through a separate read-only connection (ADR-0024).

**What it works on, in order.** (1) Titles someone queued, oldest first.
(2) Titles never indexed, movies before episodes, newest Plex `addedAt` first.
(3) Titles whose `file_hash` changed since they were indexed, least recently
indexed first. A title that failed is retried by neither (2) nor (3) until its
`file_hash` changes or it is queued again.

**Sampling.** The title ends at its last video frame, as the scdet pass decodes
it: a container can carry seconds of audio past the video, and a frame asked
for there does not exist. The boundaries are the Cuts, ignoring any closer than
`MIN_SHOT_MS` to the one before. Between two boundaries is a Shot, and its
`(in_ms, out_ms)` is stored in `shots`: this module is the only writer of Shot
boundaries, and readers read the boundaries it wrote. A Shot of
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
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import formatdate
from pathlib import Path
from typing import Protocol

from . import frame_quant, frames
from .config import Config
from .errors import ConfigError, EncoderError, EncoderUnavailable, MediaError, PlexdbError
from .frame_encoder import EncoderClient
from .identity import canonical_path
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
    "Scan",
    "harvest",
    "local_path",
    "locator",
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
class Scan:
    """What one decode of a title finds: its Cuts and the offset of its last video frame."""

    cuts: list[Cut]
    last_frame_ms: int
    warnings: int = 0


@dataclass(frozen=True)
class Candidate:
    """One title the harvester may index, as Plex describes it now."""

    item_id: str
    part_id: int
    size: int
    #: The part's file as Plex names it; `local_path` says where this process reads it.
    file: str
    reason: str
    #: Already computed by the discovery of a changed title, so it is not read twice.
    file_hash: str | None = None


class MediaReader(Protocol):
    """The three things the harvester asks of a video file. `FfmpegMedia` is the
    real one; a test substitutes a fake that decodes nothing."""

    def file_hash(self, path: Path, size: int) -> str: ...

    def scan(self, path: Path) -> Scan: ...

    def frames(self, path: Path, times_ms: Sequence[int]) -> list[bytes | None]:
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


# --- reading the file -----------------------------------------------------------


def local_path(media_dir: Path, plex_file: str, source_roots: Sequence[str]) -> Path:
    """Where this process reads the file Plex reports as `plex_file`.

    Plex's path loses whichever of `source_roots` it sits under and is read below
    `media_dir`. A path under no root, or one whose `..` would leave `media_dir`, is a
    `MediaError` rather than a read somewhere else. The check is on the path's text,
    so a symlink inside the library is followed wherever it points."""
    rest = canonical_path(plex_file, source_roots)
    if rest == plex_file.replace("\\", "/"):
        raise MediaError(
            f"{plex_file} is under none of PLEX_SOURCE_ROOTS {list(source_roots)}",
            "file not under PLEX_SOURCE_ROOTS",
        )
    root = Path(os.path.normpath(media_dir.absolute()))
    path = Path(os.path.normpath(root / rest))
    if path == root or not path.is_relative_to(root):
        raise MediaError(f"{plex_file} leaves PLEXDB_MEDIA_DIR", "file leaves PLEXDB_MEDIA_DIR")
    return path


class FfmpegMedia:
    """Reads a part's file with ffmpeg and ffprobe, read-only, from the media mount.

    Reading the file rather than `/library/parts/<id>/file` matters: Plex makes each
    such read a playback session and ends the account's previous one, so two readers
    of different titles cut each other off."""

    def __init__(
        self,
        *,
        ffmpeg: str,
        ffprobe: str,
        decode: str = "cpu",
        on_fallback: Callable[[str], None] | None = None,
    ) -> None:
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._decode = decode
        self._on_fallback = on_fallback

    @property
    def decoder(self) -> str:
        """`cuda` while CUDA decodes, `cpu` otherwise, including after a fallback."""
        return self._decode

    def _hwaccel(self) -> list[str]:
        """Flags before the scene scan's input; extraction never takes them, because 24
        parallel 4K CUDA decoders exceed NVDEC's session limits (docs/schema.md has the
        measurements). No `-hwaccel_output_format`: ffmpeg copies each decoded frame back to
        system memory, so scdet and framecrc see the pixels the CPU decode gives."""
        return ["-hwaccel", "cuda"] if self._decode == "cuda" else []

    def _fall_back(self, stderr: str) -> None:
        """CUDA failed where a CPU decode is about to be tried: decode on the CPU from here
        on and say why, with ffmpeg's own last line."""
        lines = [line for line in stderr.strip().splitlines() if line.strip()]
        reason = lines[-1].strip() if lines else "ffmpeg exited without a message"
        self._decode = "cpu"
        if self._on_fallback is not None:
            self._on_fallback(reason)

    def file_hash(self, path: Path, size: int) -> str:
        """sha256 over the part's size, its modification time and its first MiB."""
        try:
            stat = path.stat()
            with path.open("rb") as handle:
                head = handle.read(_HASH_BYTES)
        except OSError as err:
            raise MediaError(f"{path}: cannot read: {err.strerror}", "file unreadable") from None
        modified = formatdate(int(stat.st_mtime), usegmt=True)
        digest = hashlib.sha256(f"{size}|{modified}|".encode())
        digest.update(head)
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
            raise MediaError(
                f"{what}: {argv[0]} is not installed", "ffmpeg or ffprobe not installed"
            ) from None
        except subprocess.TimeoutExpired:
            raise MediaError(
                f"{what}: {argv[0]} ran past {timeout_s:.0f} s", f"{Path(argv[0]).name} timed out"
            ) from None

    def scan(self, path: Path) -> Scan:
        scdet, last_frame_ms, warnings = self._scdet(path)
        found = sorted(scdet + self._chapters(path), key=lambda cut: (cut.t_ms, cut.source))
        return Scan(found, last_frame_ms, warnings)

    def _scdet(self, path: Path) -> tuple[list[Cut], int, int]:
        """One decode, one chain: scdet marks the Cuts and prints them, and passes every frame
        on (`s=0`) to framecrc, which records each frame's pts.

        Not a `split` into a scdet branch and a framecrc branch: ffmpeg then queues every
        decoded frame until the file ends, so memory grows with the title's length and a 4K
        10-bit film is killed before it finishes."""
        with tempfile.TemporaryDirectory(prefix="plexdb-scan-") as tmp:
            crc = Path(tmp) / "frames.crc"

            def argv() -> list[str]:
                return [
                    self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "warning",
                    *self._hwaccel(), "-i", str(path),
                    "-filter_complex",
                    "[0:v:0]scale=-2:224,scdet=t=10:s=0,metadata=print:file=-[all]",
                    "-map", "[all]", "-fps_mode", "passthrough", "-f", "framecrc",
                    "-y", str(crc),
                ]  # fmt: skip

            done = self._run(argv(), f"{path}: scdet", timeout_s=_DECODE_TIMEOUT_S)
            if done.returncode != 0 and self._decode == "cuda":
                cuda_stderr = done.stderr
                self._decode = "cpu"
                done = self._run(argv(), f"{path}: scdet", timeout_s=_DECODE_TIMEOUT_S)
                if done.returncode == 0:
                    self._fall_back(cuda_stderr)
                else:
                    self._decode = "cuda"
            if done.returncode != 0:
                raise MediaError(
                    f"{path}: ffmpeg scdet exited {done.returncode}: {done.stderr.strip()[-200:]}",
                    f"ffmpeg scene scan exited {done.returncode}",
                )
            errors, warnings = classify_decode_stderr(done.stderr)
            if errors:
                raise MediaError(
                    f"{path}: source file damaged ({len(errors)} decode errors, "
                    f"{self._decode} decode); first {_ERROR_LINES_KEPT}: "
                    + " | ".join(errors[:_ERROR_LINES_KEPT]),
                    f"source file damaged ({len(errors)} decode errors)",
                )
            with crc.open() as lines:
                last_frame_ms = parse_last_frame(lines)
        if last_frame_ms is None:
            raise MediaError(f"{path}: ffmpeg decoded no video frame", "no video frame decoded")
        return parse_scdet(done.stdout), last_frame_ms, warnings

    def _chapters(self, path: Path) -> list[Cut]:
        argv = [
            self._ffprobe, "-v", "error", "-show_chapters", "-print_format", "json", str(path),
        ]  # fmt: skip
        done = self._run(argv, f"{path}: chapters")
        if done.returncode != 0:
            raise MediaError(
                f"{path}: ffprobe exited {done.returncode}: {done.stderr.strip()[-200:]}",
                f"ffprobe exited {done.returncode}",
            )
        return parse_chapters(done.stdout)

    def frames(
        self, path: Path, times_ms: Sequence[int], *, height: int = 224
    ) -> list[bytes | None]:
        out: list[bytes | None] = []
        for start in range(0, len(times_ms), _EXTRACT_CHUNK):
            chunk = times_ms[start : start + _EXTRACT_CHUNK]
            with tempfile.TemporaryDirectory(prefix="plexdb-frames-") as tmp:

                def cut(
                    chunk: Sequence[int] = chunk, tmp: str = tmp
                ) -> tuple[subprocess.CompletedProcess[str], list[bytes | None]]:
                    argv = [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
                    # One decoder thread per input: each of the chunk's inputs is its own
                    # decoder, and at the default of one thread per core a 4K chunk peaked at
                    # 16 GB (4 GB at one thread, and faster, since the inputs already run side
                    # by side).
                    for t_ms in chunk:
                        argv += [
                            "-threads", "1",
                            "-ss", f"{t_ms / 1000:.3f}", "-i", str(path),
                        ]  # fmt: skip
                    for i in range(len(chunk)):
                        argv += [
                            "-map", f"{i}:v:0", "-frames:v", "1", "-vf", f"scale=-2:{height}",
                            "-q:v", "3", "-y", str(Path(tmp) / f"{i}.jpg"),
                        ]  # fmt: skip
                    ran = self._run(argv, f"{path}: frames")
                    jpgs = [Path(tmp) / f"{i}.jpg" for i in range(len(chunk))]
                    return ran, [jpg.read_bytes() if jpg.exists() else None for jpg in jpgs]

                done, found = cut()
            # One ffmpeg cutting several inputs ends early when one input is past the file's
            # end, which can leave a time inside the file without its JPEG. Cut each missing
            # time alone, so a miss means that time has no frame.
            if len(chunk) > 1:
                for i, t_ms in enumerate(chunk):
                    if found[i] is None:
                        found[i] = self._frame_alone(path, t_ms, height)
            if done.returncode != 0 and not any(found):
                raise MediaError(
                    f"{path}: ffmpeg exited {done.returncode}: {done.stderr.strip()[-200:]}",
                    f"ffmpeg frame cut exited {done.returncode}",
                )
            out += found
        return out

    def _frame_alone(self, path: Path, t_ms: int, height: int) -> bytes | None:
        with tempfile.TemporaryDirectory(prefix="plexdb-frame-") as tmp:
            target = Path(tmp) / "0.jpg"
            argv = [
                self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-threads", "1",
                "-ss", f"{t_ms / 1000:.3f}", "-i", str(path),
                "-map", "0:v:0", "-frames:v", "1", "-vf", f"scale=-2:{height}",
                "-q:v", "3", "-y", str(target),
            ]  # fmt: skip
            self._run(argv, f"{path}: frame at {t_ms} ms")  # no JPEG: no frame at this time
            return target.read_bytes() if target.exists() else None

    def clip(self, path: Path, in_ms: int, out_ms: int, *, width: int = 640) -> bytes:
        """The file's `[in_ms, out_ms)` window as a silent H.264 MP4 `width` px wide.

        The Search tab plays one Shot as a looping clip. `+faststart` moves the index
        to the front, so a `<video>` starts before the whole file has arrived.
        """
        if out_ms <= in_ms:
            raise MediaError(
                f"{path}: clip window {in_ms}-{out_ms} ms is empty", "clip window empty"
            )
        with tempfile.TemporaryDirectory(prefix="plexdb-clip-") as tmp:
            target = Path(tmp) / "clip.mp4"
            argv = [
                self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-ss", f"{in_ms / 1000:.3f}", "-i", str(path),
                "-t", f"{(out_ms - in_ms) / 1000:.3f}",
                "-map", "0:v:0", "-an", "-sn", "-dn",
                "-vf", f"scale={width}:-2",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", "-y", str(target),
            ]  # fmt: skip
            done = self._run(argv, f"{path}: clip")
            if done.returncode != 0 or not target.exists():
                raise MediaError(
                    f"{path}: ffmpeg exited {done.returncode}: {done.stderr.strip()[-200:]}",
                    f"ffmpeg clip cut exited {done.returncode}",
                )
            return target.read_bytes()


_ERROR_LINES_KEPT = 5
_VIDEO_DECODER_LINE = re.compile(
    r"^\[(hevc|h264|av1|vp[89]|mpeg[124]video|mpeg4|vc1|wmv[123]|msmpeg4v[123]|mjpeg|prores"
    r"|dnxhd|theora)\w*\s*@"
)
_DECODE_ERROR_WORDS = ("invalid", "error", "outside the valid range", "undecodable", "concealing")


def classify_decode_stderr(stderr: str) -> tuple[list[str], int]:
    """Split ffmpeg's warning-level stderr into the video decoder's error lines and a count of
    every other line. A decoder line counts as an error when it carries one of
    `_DECODE_ERROR_WORDS`; the rest are warnings the harvest ignores."""
    errors: list[str] = []
    warnings = 0
    for raw in stderr.splitlines():
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower()
        if (
            _VIDEO_DECODER_LINE.match(line)
            and "failed setup for format" not in lowered
            and any(w in lowered for w in _DECODE_ERROR_WORDS)
        ):
            errors.append(line)
        else:
            warnings += 1
    return errors, warnings


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
                raise MediaError(
                    f"scdet printed an unreadable score: {line!r}", "scene scan output unreadable"
                ) from None
        elif line.startswith("lavfi.scd.time="):
            try:
                t_ms = round(float(line.split("=", 1)[1]) * 1000)
            except ValueError:
                raise MediaError(
                    f"scdet printed an unreadable time: {line!r}", "scene scan output unreadable"
                ) from None
            if t_ms > 0:
                cuts.append(Cut(t_ms, score, "scdet"))
    return cuts


def parse_last_frame(lines: Iterable[str]) -> int | None:
    """The largest frame pts in framecrc output, in whole ms rounded down; `None` with no frame.

    framecrc writes `#tb 0: <num>/<den>` and then one `stream, dts, pts, duration, size, crc`
    line per frame."""
    num = den = 0
    last: int | None = None
    for line in lines:
        if line.startswith("#tb 0:"):
            num, den = (int(part) for part in line.split(":", 1)[1].strip().split("/"))
        elif line and not line.startswith("#"):
            try:
                pts = int(line.split(",")[2])
            except (IndexError, ValueError):
                raise MediaError(
                    f"framecrc printed an unreadable line: {line!r}", "frame list unreadable"
                ) from None
            last = pts if last is None else max(last, pts)
    if last is None:
        return None
    if den <= 0:
        raise MediaError("framecrc printed no time base", "frame list unreadable")
    return last * num * 1000 // den


def parse_chapters(text: str) -> list[Cut]:
    try:
        body = json.loads(text or "{}")
        marks = [round(float(ch["start_time"]) * 1000) for ch in body.get("chapters", [])]
    except (ValueError, KeyError, TypeError):
        raise MediaError(
            "ffprobe returned chapters that are not JSON", "chapter list unreadable"
        ) from None
    return [Cut(t_ms, None, "chapter") for t_ms in marks if t_ms > 0]


# --- discovery ------------------------------------------------------------------


#: `(kind, added_at, part_id, size, file)` of one title's Plex part.
_Part = tuple[str, int, int, int, str]


def _plex_parts(store_conn: sqlite3.Connection, plex: PlexSource) -> dict[str, _Part]:
    """`item_id -> (kind, added_at, part_id, size, file)` for every movie and episode
    Plex lists with a media part. A title with several Plex copies takes the newest."""
    wanted: dict[str, tuple[str, str, str]] = {}
    for row in store_conn.execute(
        "SELECT p.rating_key, p.item_id, p.section_id, i.type FROM plex_items p "
        "JOIN items i ON i.item_id = p.item_id WHERE i.type IN ('movie', 'episode')"
    ):
        wanted[str(row[0])] = (str(row[1]), str(row[2]), str(row[3]))
    section_types = {section.key: section.type for section in plex.sections()}
    best: dict[str, _Part] = {}
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
            entry = (
                item_type,
                int(record.get("addedAt") or 0),
                int(parts[0]["id"]),
                int(parts[0].get("size") or 0),
                str(parts[0].get("file") or ""),
            )
            if item_id not in best or entry[1] > best[item_id][1]:
                best[item_id] = entry
    return best


def _candidates(
    frames_conn: sqlite3.Connection,
    parts: dict[str, _Part],
    media: MediaReader,
    locate: Callable[[str], Path],
    log: Callable[[str], None],
    keep_going: Callable[[], bool],
) -> Iterator[Candidate]:
    """Every title to index, in order, queued first. Read lazily: a queue row added
    during a run is seen before the backlog resumes."""

    def candidate(item_id: str, reason: str, file_hash: str | None = None) -> Candidate:
        _, _, part_id, size, file = parts[item_id]
        return Candidate(item_id, part_id, size, file, reason, file_hash)

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
            current = media.file_hash(locate(parts[item_id][4]), parts[item_id][3])
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
    duration_ms: int,
    shot_list: Sequence[tuple[int, int]],
    samples: Sequence[tuple[int, int]],
    vectors: Sequence[Sequence[float]],
    shot_count: int,
    model: str,
    now: datetime,
) -> None:
    with frames_conn:
        # By rowid: `DELETE FROM frame_vec WHERE item_id = ?` scans the whole vec0 table, which
        # at full library is tens of millions of vectors for every title written.
        frames.delete_vectors(frames_conn, cand.item_id)
        for table in ("cuts", "shots"):
            frames_conn.execute(f"DELETE FROM {table} WHERE item_id = ?", (cand.item_id,))  # noqa: S608
        frames_conn.executemany(
            "INSERT OR IGNORE INTO cuts (item_id, t_ms, score, source) VALUES (?, ?, ?, ?)",
            [(cand.item_id, c.t_ms, c.score, c.source) for c in cuts],
        )
        frames_conn.executemany(
            "INSERT INTO shots (item_id, shot_ix, in_ms, out_ms) VALUES (?, ?, ?, ?)",
            [(cand.item_id, ix, start, end) for ix, (start, end) in enumerate(shot_list)],
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
                cand.item_id, cand.part_id, model, frames.FRAME_DIMS, _QUANT, duration_ms,
                cand.size, file_hash, shot_count, len(samples), now.isoformat(),
            ),
        )  # fmt: skip
        frames_conn.execute("DELETE FROM frame_queue WHERE item_id = ?", (cand.item_id,))


def _record_failure(
    frames_conn: sqlite3.Connection,
    cand: Candidate,
    error: str,
    reason: str,
    file_hash: str | None,
    now: datetime,
) -> None:
    """Keep whatever rows the title had; mark the attempt failed and take it off the queue.
    `error` is the full message, `reason` the path-free phrase a page may show."""
    with frames_conn:
        frames_conn.execute(
            "INSERT INTO frame_index_state (item_id, part_id, bytes, file_hash, "
            "indexed_at, error, reason) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET part_id = excluded.part_id, "
            "bytes = excluded.bytes, file_hash = coalesce(excluded.file_hash, file_hash), "
            "indexed_at = excluded.indexed_at, error = excluded.error, "
            "reason = excluded.reason",
            (
                cand.item_id,
                cand.part_id,
                cand.size,
                file_hash,
                now.isoformat(),
                error,
                reason,
            ),
        )
        frames_conn.execute("DELETE FROM frame_queue WHERE item_id = ?", (cand.item_id,))


def _stamp(now_fn: Callable[[], datetime]) -> str:
    return now_fn().isoformat(timespec="seconds")


def _flushed(line: str) -> None:
    """The default log: a harvest whose stdout is a file shows its progress as it runs."""
    print(line, flush=True)


def _index_title(
    frames_conn: sqlite3.Connection,
    cand: Candidate,
    media: MediaReader,
    locate: Callable[[str], Path],
    encoder: EncoderClient,
    now_fn: Callable[[], datetime],
    log: Callable[[str], None],
    float_conn: sqlite3.Connection | None = None,
) -> str:
    """Index one title. Returns `"ok"`, `"failed"` (an unreadable file; recorded), or
    `"rejected"` (the encoder refused its frames; recorded). Raises `EncoderUnavailable`
    with nothing written when the encoder stops answering."""
    file_hash = cand.file_hash
    shot_count = vector_count = decode_warnings = 0
    decode_s = scan_s = extract_s = encode_s = 0.0
    error: str | None = None
    outcome = "ok"
    try:
        path = locate(cand.file)
        file_hash = file_hash or media.file_hash(path, cand.size)
        started = time.perf_counter()
        log(f"{_stamp(now_fn)} item_id={cand.item_id} stage=scan start")
        scan = media.scan(path)
        scan_s = time.perf_counter() - started
        decode_warnings = scan.warnings
        cuts = scan.cuts
        shot_list = shots([c.t_ms for c in cuts], scan.last_frame_ms)
        samples = sample_times(shot_list)
        log(f"{_stamp(now_fn)} item_id={cand.item_id} stage=extract start")
        extract_started = time.perf_counter()
        jpegs = media.frames(path, [t_ms for t_ms, _ in samples])
        extract_s = time.perf_counter() - extract_started
        decode_s = time.perf_counter() - started
        kept = [(sample, jpeg) for sample, jpeg in zip(samples, jpegs, strict=True) if jpeg]
        if not kept:
            raise MediaError(f"{path}: no frame could be read", "no frame could be read")
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
        if float_conn is not None:
            # A measurement file failing to take a title must not cost the index the
            # title; `frames quant-agreement` names the titles it holds no floats for.
            try:
                frame_quant.write_floats(float_conn, cand.item_id, samples, vectors)
            except sqlite3.Error as err:
                log(f"item_id={cand.item_id} float vectors not kept: {err}")
        _write_title(
            frames_conn, cand, file_hash=file_hash, cuts=cuts,
            duration_ms=scan.last_frame_ms, shot_list=shot_list, samples=samples,
            vectors=vectors, shot_count=shot_count, model=model, now=now_fn(),
        )  # fmt: skip
    except EncoderUnavailable:
        raise
    except (MediaError, EncoderError) as err:
        error = str(err)
        if isinstance(err, MediaError):
            outcome, reason = "failed", err.reason
        else:
            outcome, reason = "rejected", "encoder refused the frames"
        _record_failure(frames_conn, cand, error, reason, file_hash, now_fn())
    line = (
        f"{_stamp(now_fn)} item_id={cand.item_id} part={cand.part_id} "
        f"bytes={cand.size} decode_s={decode_s:.1f} scan_s={scan_s:.1f} "
        f"extract_s={extract_s:.1f} encode_s={encode_s:.1f} "
        f"shots={shot_count} vectors={vector_count} decode_warnings={decode_warnings} "
        f"reason={cand.reason}"
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
    keep_float: bool = False,
    log: Callable[[str], None] = _flushed,
) -> HarvestResult:
    """One harvest run over the configured Frame index. Raises `HarvesterBusy` when
    another harvester, in this process or another, holds the slot. `keep_float` also
    writes each title's float32 vectors to `frames-float.db` (`plexdb/frame_quant.py`)."""
    path = frames.frames_path(config)
    with frames.harvest_lock(path), ExitStack() as stack:
        float_conn = (
            stack.enter_context(frame_quant.open_float(frame_quant.float_path(path)))
            if keep_float
            else None
        )
        return _harvest_locked(
            config,
            path,
            plex=plex,
            media=media,
            encoder=encoder,
            now_fn=now_fn,
            ignore_hours=ignore_hours,
            limit=limit,
            log=log,
            float_conn=float_conn,
        )


def _harvest_locked(
    config: Config,
    path: Path,
    *,
    plex: PlexSource,
    media: MediaReader,
    encoder: EncoderClient,
    now_fn: Callable[[], datetime],
    ignore_hours: bool,
    limit: int | None,
    log: Callable[[str], None],
    float_conn: sqlite3.Connection | None,
) -> HarvestResult:
    window = frames.parse_hours(config.frame_hours)
    locate = locator(config)
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
            locate,
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
                        locate,
                        encoder,
                        now_fn,
                        lambda line: log(f"frames: {line}"),
                        float_conn,
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
    log: Callable[[str], None] = _flushed,
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
            "(or named by PLEXDB_FFMPEG and PLEXDB_FFPROBE) to harvest frames",
            "ffmpeg or ffprobe not installed",
        )
    frames_file = frames.frames_path(config)
    frames.clear_decoder_fallback(frames_file)

    def record(reason: str) -> None:
        print(f"frame decode: cuda failed, decoding on the CPU: {reason}", flush=True)
        frames.write_decoder_fallback(frames_file, reason)

    return FfmpegMedia(
        ffmpeg=config.ffmpeg,
        ffprobe=config.ffprobe,
        decode=config.frame_decode,
        on_fallback=record,
    )


def locator(config: Config) -> Callable[[str], Path]:
    """`local_path` under the configured media directory, or a `ConfigError` when
    there is none: frame work reads files, so it needs to know where they are."""
    media_dir = config.media_dir
    if media_dir is None:
        raise ConfigError(
            "PLEXDB_MEDIA_DIR is not set, so no video file can be read; mount the library "
            "read-only and name the directory Plex calls PLEX_SOURCE_ROOTS"
        )
    if not config.source_roots:
        raise ConfigError("PLEX_SOURCE_ROOTS is not set, so Plex's file paths cannot be mapped")
    return lambda plex_file: local_path(media_dir, plex_file, config.source_roots)
