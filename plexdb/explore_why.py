"""Why a Shot matched: the Search tab's "Why?" inspector, computed from `frames.db`.

Everything here is arithmetic on vectors the Frame index already holds plus the phrase
vector the search left in the Phrase cache, so no encoder or model weights are involved
(ADR-0017). A frame's similarity to the phrase is `dot(phrase, frame) / |frame|`, the same
cosine `plexdb-read search` ranks by; two frames' image similarity is the cosine of their
int8 vectors.

The library's vectors are read once and kept until a title is indexed or replaced: 70,083 frames of
768 int8s are about 54 MB.
"""

from __future__ import annotations

import sqlite3
import struct
import threading
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import sqlite_vec

from .frames import FRAME_DIMS

#: Peaks marked on a title's plot, and the least gap between two of them.
PEAKS = 4
PEAK_GAP_MS = 120_000
#: Nearest frames shown from the same title and from other titles, and the cap per other title.
SAME_TITLE = 6
OTHER_TITLES = 8
PER_TITLE = 2
#: Rows multiplied at once, so the float32 copy of the int8 matrix stays small.
CHUNK = 8192


class WhyUnavailable(Exception):
    """The match cannot be explained; the message says why."""


@dataclass
class Library:
    """Every indexed frame: where it is, its int8 vector, and the vector's length."""

    stamp: tuple[object, ...]
    item_ids: list[str]
    t_ms: np.ndarray
    shot_ix: np.ndarray
    vecs: np.ndarray  # int8, (n, FRAME_DIMS)
    norms: np.ndarray  # float32, (n,)
    first: dict[str, tuple[int, int]]  # item_id -> [start, end) rows, contiguous


def _dots(vecs: np.ndarray, query: np.ndarray) -> np.ndarray:
    out = np.empty(len(vecs), dtype=np.float32)
    for start in range(0, len(vecs), CHUNK):
        out[start : start + CHUNK] = vecs[start : start + CHUNK].astype(np.float32) @ query
    return out


def _cosines(lib: Library, query: np.ndarray, *, unit: bool = False) -> np.ndarray:
    """Each frame's cosine with `query`; 0 for an all-zero frame. With `unit` the query is taken
    as already a unit vector, as `plexdb-read` takes a phrase: `dot(phrase, frame) / |frame|`."""
    size = 1.0 if unit else float(np.linalg.norm(query))
    dots = _dots(lib.vecs, query.astype(np.float32))
    denom = lib.norms * size
    out: np.ndarray = np.divide(dots, denom, out=np.zeros_like(dots), where=denom > 0)
    return out


class WhyCache:
    """The library read from `frames.db`, once per change to the file."""

    def __init__(self, frames_file: Callable[[], Path]) -> None:
        self._frames_file = frames_file
        self._lock = threading.Lock()
        self._library: Library | None = None

    def library(self) -> Library:
        path = self._frames_file()
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)) as conn:
            # The vectors change only when a title is indexed or replaced, which moves one of these;
            # the file itself is written to all through a harvest.
            stamp = tuple(
                conn.execute(
                    "SELECT count(*), coalesce(sum(vectors), 0), coalesce(max(indexed_at), '') "
                    "FROM frame_index_state WHERE model IS NOT NULL AND error IS NULL"
                ).fetchone()
            )
        with self._lock:
            if self._library is None or self._library.stamp != stamp:
                self._library = _load(path, stamp)
            return self._library


def _load(path: Path, stamp: tuple[object, ...]) -> Library:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        rows = conn.execute(
            "SELECT v.item_id, v.t_ms, v.shot_ix, v.vec FROM frame_vec v "
            "JOIN frame_index_state s ON s.item_id = v.item_id "
            "WHERE s.model IS NOT NULL AND s.error IS NULL"
        ).fetchall()
    finally:
        conn.close()
    rows.sort(key=lambda r: (r[0], r[1]))
    n = len(rows)
    vecs = np.empty((n, FRAME_DIMS), dtype=np.int8)
    first: dict[str, tuple[int, int]] = {}
    for i, (item_id, _t, _ix, blob) in enumerate(rows):
        vecs[i] = np.frombuffer(blob, dtype=np.int8)
        start = first[item_id][0] if item_id in first else i
        first[item_id] = (start, i + 1)
    norms = np.sqrt(np.square(vecs.astype(np.float32)).sum(axis=1))
    return Library(
        stamp=stamp,
        item_ids=[r[0] for r in rows],
        t_ms=np.array([r[1] for r in rows], dtype=np.int64),
        shot_ix=np.array([r[2] for r in rows], dtype=np.int64),
        vecs=vecs,
        norms=norms.astype(np.float32),
        first=first,
    )


def phrase_vector(cache_file: Path | None, phrase: str, model: str, version: str) -> np.ndarray:
    """The vector `plexdb-read search` cached for `phrase`, which it embeds before ranking."""
    if cache_file is None or not cache_file.exists():
        raise WhyUnavailable("the Phrase cache is missing; search again")
    surface = " ".join(phrase.lower().split())
    with closing(sqlite3.connect(f"file:{cache_file}?mode=ro", uri=True, timeout=10.0)) as conn:
        row = conn.execute(
            "SELECT vec FROM phrase_cache WHERE phrase = ? AND model = ? AND model_version = ?",
            (surface, model, version),
        ).fetchone()
    if row is None or len(row[0]) != FRAME_DIMS * 4:
        raise WhyUnavailable("this phrase is not in the Phrase cache; search again")
    return np.array(struct.unpack(f"<{FRAME_DIMS}f", row[0]), dtype=np.float32)


def explain(
    lib: Library,
    phrase_vec: np.ndarray,
    item_id: str,
    shot_ix: int,
) -> dict[str, Any]:
    """The numbers behind one Shot's match, before titles and parts are added."""
    if item_id not in lib.first:
        raise WhyUnavailable(f"{item_id} has no frames in the Frame index")
    sims = _cosines(lib, phrase_vec, unit=True)
    median = float(np.median(sims))
    top1 = float(np.quantile(sims, 0.99))
    highest = float(sims.max())
    lo, hi = lib.first[item_id]
    title_sims = sims[lo:hi]
    in_shot = np.flatnonzero(lib.shot_ix[lo:hi] == shot_ix)
    if len(in_shot) == 0:
        raise WhyUnavailable("this Shot has no frames in the Frame index")
    best = lo + int(in_shot[np.argmax(title_sims[in_shot])])
    best_sim = float(sims[best])

    series = [[int(t), float(s)] for t, s in zip(lib.t_ms[lo:hi], title_sims, strict=True)]
    peaks: list[dict[str, Any]] = []
    for i in np.argsort(-title_sims, kind="stable"):
        t = int(lib.t_ms[lo + i])
        if all(abs(t - p["t_ms"]) >= PEAK_GAP_MS for p in peaks):
            peaks.append({"t_ms": t, "sim": float(title_sims[i])})
            if len(peaks) == PEAKS:
                break

    likeness = _cosines(lib, lib.vecs[best].astype(np.float32))
    same: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    per_title: dict[str, int] = {}
    for i in np.argsort(-likeness, kind="stable"):
        i = int(i)
        if i == best:
            continue
        other_id = lib.item_ids[i]
        entry = {
            "item_id": other_id,
            "t_ms": int(lib.t_ms[i]),
            "image_sim": float(likeness[i]),
            "phrase_sim": float(sims[i]),
        }
        if other_id == item_id:
            if lib.shot_ix[i] != shot_ix and len(same) < SAME_TITLE:
                same.append(entry)
        elif len(other) < OTHER_TITLES and per_title.get(other_id, 0) < PER_TITLE:
            per_title[other_id] = per_title.get(other_id, 0) + 1
            other.append(entry)
        if len(same) == SAME_TITLE and len(other) == OTHER_TITLES:
            break
    return {
        "frames": int(len(sims)),
        "titles": len(lib.first),
        "median": median,
        "top1": top1,
        "highest": highest,
        "is_highest": best_sim >= highest,
        "best": {"t_ms": int(lib.t_ms[best]), "sim": best_sim},
        "series": series,
        "peaks": peaks,
        "same": same,
        "other": other,
    }
