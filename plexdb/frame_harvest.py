"""The frame harvester's Python side: the schedule's window loop around `plexdb-rs
harvest-frames`, and the reads of a video file the Search tab and the file checks still make.

The harvester itself (`crates/plexdb/src/harvest_frames.rs`) reads each title's file, finds
its Cuts and Shots, embeds the sampled frames and replaces the title's rows in `frames.db`
(ADR-0024, ADR-0025). `plexdb schedule` starts `run_in_windows`, which runs it whenever
`PLEXDB_FRAME_HOURS` is open; the binary stops when the window closes, the work runs out, at
`--limit`, or when the encoder does not answer, and prints why in its last line.

`FfmpegMedia` is the other half: ffprobe and ffmpeg over a part's file under
`PLEXDB_MEDIA_DIR`, read-only. The Search tab cuts a frame or a clip with it
(`plexdb/explore_search.py`) and the file checks probe and decode with it
(`plexdb/media_check.py`).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from . import frames, rust
from .config import Config
from .errors import ConfigError, MediaError, MediaTimeout, PlexdbError
from .identity import canonical_path

__all__ = [
    "FfmpegMedia",
    "harvest",
    "local_path",
    "locator",
    "run_in_windows",
]

#: Frames cut out per ffmpeg process.
_EXTRACT_CHUNK = 24
#: The longest one ffmpeg or ffprobe process may run: a full decode for the file check,
#: minutes for anything else.
_DECODE_TIMEOUT_S = 6 * 3600
_PROBE_TIMEOUT_S = 900

#: Why a run stopped, as `harvest-frames` prints it (`crates/plexdb/src/harvest_frames.rs`).
_STOP_EMPTY = "nothing to do"
_STOP_ENCODER = "encoder unavailable"
_STOP_REJECTED = "encoder rejected consecutive titles"
#: The binary's last line: `harvest-frames stopped: <why>; <n> indexed, <n> failed`.
_SUMMARY = "harvest-frames stopped: "


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

    def __init__(self, *, ffmpeg: str, ffprobe: str) -> None:
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe

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
            raise MediaTimeout(
                f"{what}: {argv[0]} ran past {timeout_s:.0f} s", f"{Path(argv[0]).name} timed out"
            ) from None

    def probe(self, path: Path) -> tuple[dict[str, Any] | None, str]:
        """ffprobe's streams and format for `path`: `(report, "")`, or `(None, why)` when
        ffprobe ran and rejected the file (`why` is its last stderr line, without the path).
        A missing binary or a run past the time limit (`MediaTimeout`) is a `MediaError`,
        since the file
        itself was not judged."""
        argv = [
            self._ffprobe, "-v", "error", "-show_format", "-show_streams",
            "-print_format", "json", str(path),
        ]  # fmt: skip
        done = self._run(argv, f"{path}: probe")
        if done.returncode != 0:
            lines = [ln for ln in done.stderr.strip().splitlines() if ln.strip()]
            why = lines[-1].replace(f"{path}: ", "").strip() if lines else ""
            return None, why or f"ffprobe exited {done.returncode}"
        try:
            report = json.loads(done.stdout or "{}")
        except ValueError:
            return None, "ffprobe printed output that is not JSON"
        return (report, "") if isinstance(report, dict) else (None, "ffprobe printed no report")

    def decode(self, path: Path) -> str:
        """Decode every frame of `path` and throw it away: `""` when ffmpeg reported nothing,
        else its error output with the path replaced by `<file>`. A missing binary is a
        `MediaError`, since the file itself was not judged; a run past the time limit is a
        `MediaTimeout`."""
        argv = [
            self._ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-f", "null", "-",
        ]  # fmt: skip
        done = self._run(argv, f"{path}: decode", timeout_s=_DECODE_TIMEOUT_S)
        lines = [ln.replace(str(path), "<file>").strip() for ln in done.stderr.splitlines()]
        lines = [ln for ln in lines if ln]
        if done.returncode != 0 and not lines:
            return f"ffmpeg exited {done.returncode}"
        return "\n".join(lines[:5])

    def frames(
        self, path: Path, times_ms: Sequence[int], *, height: int = 224
    ) -> list[bytes | None]:
        """One JPEG per time, `None` where the file has no frame there."""
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
            # An input seeked past the file's last frame writes no JPEG and makes ffmpeg exit
            # non-zero; every other input still writes its own, so a None is that time's miss
            # (tests/test_frame_harvest.py runs a real ffmpeg over a past-the-end time).
            if done.returncode != 0 and not any(found):
                raise MediaError(
                    f"{path}: ffmpeg exited {done.returncode}: {done.stderr.strip()[-200:]}",
                    f"ffmpeg frame cut exited {done.returncode}",
                )
            out += found
        return out

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


# --- the run ----------------------------------------------------------------------


def _flushed(line: str) -> None:
    """The default log: a harvest whose stdout is a file shows its progress as it runs."""
    print(line, flush=True)


def harvest(config: Config, log: Callable[[str], None] = _flushed) -> str:
    """One `plexdb-rs harvest-frames` run, its lines passed to `log` as they print. Returns
    why it stopped (the binary's last line names it). Raises `RustStepError` when the binary
    fails, and `ConfigError` when a setting it needs is absent."""
    stopped = ""

    def on_line(line: str) -> None:
        nonlocal stopped
        log(line)
        if line.startswith(_SUMMARY):
            stopped = line.removeprefix(_SUMMARY).split(";", 1)[0]

    rust.stream(["harvest-frames"], on_line, config)
    return stopped


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
