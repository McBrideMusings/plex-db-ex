"""The int8-against-fp16 measurement for the Frame index (plex-db-ex-8nb.1.7).

`frame_vec` keeps each frame vector only as int8. To say what that costs, a
harvest run with `--keep-float` also writes the encoder's float32 vectors to
`frames-float.db` beside `frames.db`, and `agreement` ranks the same Shots for a
phrase from both and counts how many of the top k match. The file is a
measurement, not part of the Frame index: no reader opens it, nothing migrates
it, and it can be deleted at any time.

Both rankings here use the search's phrase-only rule — a Shot scores its best
frame's cosine with the phrase — so the only difference between them is how the
frame vector was stored. The fp16 side casts each float32 vector to float16.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .frames import FRAME_DIMS
from .store import BUSY_TIMEOUT_MS

__all__ = ["FLOAT_FILE", "Paired", "agreement", "float_path", "load", "open_float"]

FLOAT_FILE = "frames-float.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS frame_float (
    item_id TEXT NOT NULL,
    t_ms    INTEGER NOT NULL,
    shot_ix INTEGER NOT NULL,
    vec     BLOB NOT NULL,      -- FRAME_DIMS little-endian float32s, as the encoder sent them
    PRIMARY KEY (item_id, t_ms)
);
"""


def float_path(frames_db: Path) -> Path:
    return frames_db.parent / FLOAT_FILE


@contextmanager
def open_float(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.executescript(_SCHEMA)
        yield conn
    finally:
        conn.close()


def _shot_scores(keys: list[tuple[str, int]], sims: np.ndarray) -> dict[tuple[str, int], float]:
    best: dict[tuple[str, int], float] = {}
    for key, sim in zip(keys, sims.tolist(), strict=True):
        if sim > best.get(key, -2.0):
            best[key] = sim
    return best


def _ranked(scores: dict[tuple[str, int], float]) -> list[tuple[str, int]]:
    return sorted(scores, key=lambda key: (-scores[key], key))


@dataclass
class Paired:
    """The frames both files hold, row-aligned: `keys[i]` is frame i's Shot."""

    keys: list[tuple[str, int]]
    int8: np.ndarray
    fp16: np.ndarray
    titles: int
    titles_without_floats: list[str]


def load(frames_conn: sqlite3.Connection, float_conn: sqlite3.Connection) -> Paired:
    """Read both files once; `agreement` then scores any number of phrases."""
    floats = {
        (row[0], row[1]): (row[2], row[3])
        for row in float_conn.execute("SELECT item_id, t_ms, shot_ix, vec FROM frame_float")
    }
    keys: list[tuple[str, int]] = []
    int8_rows: list[bytes] = []
    fp16 = np.empty((len(floats), FRAME_DIMS), dtype=np.float16)
    titles: set[str] = set()
    paired_titles: set[str] = set()
    for item_id, t_ms, shot_ix, vec in frames_conn.execute(
        "SELECT item_id, t_ms, shot_ix, vec FROM frame_vec"
    ):
        titles.add(item_id)
        held = floats.pop((item_id, t_ms), None)
        if held is None or held[0] != shot_ix:
            continue
        fp16[len(keys)] = np.frombuffer(held[1], dtype="<f4")
        keys.append((item_id, shot_ix))
        int8_rows.append(vec)
        paired_titles.add(item_id)
    int8 = np.frombuffer(b"".join(int8_rows), dtype=np.int8).reshape(-1, FRAME_DIMS)
    return Paired(keys, int8, fp16[: len(keys)], len(titles), sorted(titles - paired_titles))


def _sims(vectors: np.ndarray, phrase: np.ndarray) -> np.ndarray:
    as_f32 = vectors.astype(np.float32)
    sims: np.ndarray = as_f32 @ phrase / np.linalg.norm(as_f32, axis=1)
    return sims


def agreement(
    paired: Paired, phrase_vec: Sequence[float], *, ks: Sequence[int] = (10, 50)
) -> dict[str, Any]:
    """Rank Shots for one phrase from int8 and from fp16 and report the overlap of
    each top k."""
    report: dict[str, Any] = {
        "frames": len(paired.keys),
        "shots": 0,
        "top": {},
        "same_first": None,
        "max_abs_sim_diff": None,
    }
    if not paired.keys:
        return report
    phrase = np.asarray(phrase_vec, dtype=np.float32)
    phrase /= np.linalg.norm(phrase)
    int8_sims = _sims(paired.int8, phrase)
    fp16_sims = _sims(paired.fp16, phrase)
    int8_rank = _ranked(_shot_scores(paired.keys, int8_sims))
    fp16_rank = _ranked(_shot_scores(paired.keys, fp16_sims))
    report["shots"] = len(int8_rank)
    for k in ks:
        report["top"][str(k)] = len(set(int8_rank[:k]) & set(fp16_rank[:k]))
    report["same_first"] = int8_rank[0] == fp16_rank[0]
    report["max_abs_sim_diff"] = round(float(np.max(np.abs(int8_sims - fp16_sims))), 5)
    return report
