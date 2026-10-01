"""Finding synonym keywords: embeddings propose pairs, Jev judges them, and a
person's decisions are folded in from a file (ADR-0018).

`keyword_pairs` holds Jev's answer for every pair it was asked about, and
nothing else the writer concludes: no cosine, no status, no "counts as merged".
A pair is judged once. A pair that has a row is never asked again, and the
judge step never touches its `decision`.

**Which pairs are asked about.** Every stored keyword with no `keyword_pairs`
row on either side is *pending*. Each pending keyword proposes its 10 nearest
neighbours in the whole stored vocabulary with cosine >= `MIN_COSINE`. The
pending set is read once at the start of a run, so two pending keywords that are
each other's neighbours both still propose all their pairs.

A kill partway leaves valid rows behind, because keywords are processed in
batches of `KEYWORD_BATCH` and each batch is judged completely before its rows
are written in one transaction. The cost: a keyword that received a row only as
the other side of someone else's pair is no longer pending on the next run, so
its own remaining neighbours are not proposed by it. Nothing in the schema marks
"examined" without storing a status, which ADR-0018 forbids.

**What is embedded.** The readable surface form (`keyword_forms.surface`, the
shortest per keyword), not the stored stem, and with no task prefix. The pair is
stored under the stems, and Jev is asked about the surfaces.

**Embeddings are cached in a file, not the store.** Every keyword with no neighbour at
`MIN_COSINE` stays pending for good (a row exists only for a judged pair), so
without a cache each sweep would embed the whole vocabulary again. The cache is
`keyword-embeddings.npz` beside the store, one unit vector per surface text and
the model that made them. It is derived and disposable: deleting it, or changing
the model, costs one re-embed. A sweep embeds only the texts the cache lacks, so
a night with no new keyword makes no embedding request.

**Decisions.** The explorer never writes `plexdb.db` (ADR-0007, ADR-0017), so a
person's accept or reject reaches the table only through `merge_decisions.json`,
which `fold_merge_decisions` applies at the start of a sweep.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from .config import Config
from .embed_client import MODEL, Embedder
from .errors import MergeDecisionsError
from .explore import EXPLORE_SAVED_PATH_VAR, SAVED_FILE
from .jev_client import Judge, Verdict
from .keywords import NAMESPACE

__all__ = [
    "FoldStats",
    "JudgeStats",
    "find_synonym_pairs",
    "fold_merge_decisions",
    "merge_decisions_path",
]

#: How many nearest neighbours each pending keyword proposes.
NEIGHBOURS = 10
#: The lowest cosine a neighbour may have and still be proposed.
MIN_COSINE = 0.75
#: Texts per embeddings request.
EMBED_BATCH = 128
#: Pending keywords per committed batch — about 330 pairs, about 10 seconds of Jev.
KEYWORD_BATCH = 200
#: Jev requests in flight at once.
JUDGE_WORKERS = 8

MERGE_DECISIONS_FILE = "merge_decisions.json"
EMBEDDING_CACHE_FILE = "keyword-embeddings.npz"
_DECISIONS = ("accepted", "rejected", "cleared")

_KEY = "keyword"

_Vectors = npt.NDArray[np.float32]


@dataclass
class JudgeStats:
    keywords_stored: int = 0
    #: Stored keywords with no `keyword_pairs` row on either side when the run began.
    keywords_pending: int = 0
    #: Pending keywords this run proposed neighbours for (fewer than pending under `limit`).
    keywords_examined: int = 0
    pairs_proposed: int = 0
    #: Proposed pairs that already had a row, left alone.
    pairs_already_judged: int = 0
    pairs_judged: int = 0
    batches_committed: int = 0


@dataclass
class FoldStats:
    file_found: bool = False
    entries: int = 0
    #: Entries naming a pair that has a row, whether or not its decision changed.
    matched: int = 0
    #: Entries naming a pair with no row. Not an error: the file may name a pair a
    #: restored older store never judged.
    unmatched: int = 0


def merge_decisions_path(config: Config) -> Path:
    """`merge_decisions.json`, beside the explorer's saved-queries file.

    That file is `PLEXDB_EXPLORE_SAVED_PATH` when set (the container's own mount);
    otherwise it sits beside the published snapshot when one is configured, as the
    scheduler's explorer puts it, and beside the store otherwise, as `plexdb
    explore` does.
    """
    raw = os.environ.get(EXPLORE_SAVED_PATH_VAR, "").strip()
    if raw:
        saved = Path(raw).expanduser()
    else:
        saved = (config.snapshot_path or config.store_path).with_name(SAVED_FILE)
    return saved.with_name(MERGE_DECISIONS_FILE)


def fold_merge_decisions(conn: sqlite3.Connection, path: Path) -> FoldStats:
    """Apply `path` to `keyword_pairs.decision`; a missing file is no decisions.

    The file is a list of `{keyword_a, keyword_b, decision, decided_at}`, where
    `decision` is `accepted`, `rejected` or `cleared`. Entries apply in list
    order, so the last entry for a pair wins. `cleared` sets `decision` and
    `decided_at` back to NULL. The whole file is validated before the first row
    changes, and applied in one transaction. It is applied every run, so the
    file stays the record of the latest decision per pair.
    """
    stats = FoldStats()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return stats
    except OSError as err:
        raise MergeDecisionsError(f"cannot read {path}: {err}") from None
    stats.file_found = True
    entries = _parse_decisions(path, text)
    stats.entries = len(entries)
    with conn:
        for entry in entries:
            cleared = entry["decision"] == "cleared"
            cursor = conn.execute(
                "UPDATE keyword_pairs SET decision = ?, decided_at = ? "
                "WHERE keyword_a = ? AND keyword_b = ?",
                (
                    None if cleared else entry["decision"],
                    None if cleared else entry["decided_at"],
                    entry["keyword_a"],
                    entry["keyword_b"],
                ),
            )
            if cursor.rowcount:
                stats.matched += 1
            else:
                stats.unmatched += 1
    return stats


def _parse_decisions(path: Path, text: str) -> list[dict[str, str]]:
    try:
        raw = json.loads(text)
    except ValueError as err:
        raise MergeDecisionsError(f"{path} is not valid JSON: {err}") from None
    if not isinstance(raw, list):
        raise MergeDecisionsError(f"{path} does not hold a list of decisions")
    for index, entry in enumerate(raw):
        where = f"{path} entry {index}"
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(field), str)
            for field in ("keyword_a", "keyword_b", "decision", "decided_at")
        ):
            raise MergeDecisionsError(
                f"{where} needs text keyword_a, keyword_b, decision and decided_at"
            )
        if entry["decision"] not in _DECISIONS:
            raise MergeDecisionsError(
                f"{where} has decision {entry['decision']!r}, expected one of {_DECISIONS}"
            )
        if not entry["keyword_a"] < entry["keyword_b"]:
            raise MergeDecisionsError(f"{where} must have keyword_a < keyword_b")
        if entry["decision"] != "cleared":
            try:
                datetime.fromisoformat(entry["decided_at"])
            except ValueError:
                raise MergeDecisionsError(
                    f"{where} has decided_at {entry['decided_at']!r}, expected an ISO 8601 time"
                ) from None
    entries: list[dict[str, str]] = raw
    return entries


def find_synonym_pairs(
    conn: sqlite3.Connection,
    embedder: Embedder,
    judge: Judge,
    *,
    cache_path: Path | None = None,
    limit: int | None = None,
    log: Callable[[str], None] = lambda _line: None,
) -> JudgeStats:
    """Judge every pair the pending keywords propose and write one row per pair.

    `limit` caps how many pending keywords this run examines, so a first run
    over a whole vocabulary can be taken in pieces. `cache_path` is the
    embedding cache file; `None` embeds everything each call.
    """
    stats = JudgeStats()
    surfaces = _surfaces(conn)
    keywords = sorted(surfaces)
    stats.keywords_stored = len(keywords)

    has_row = {
        keyword
        for row in conn.execute("SELECT keyword_a, keyword_b FROM keyword_pairs")
        for keyword in (row[0], row[1])
    }
    pending = [keyword for keyword in keywords if keyword not in has_row]
    stats.keywords_pending = len(pending)
    if limit is not None:
        pending = pending[:limit]
    if not pending:
        return stats

    vectors = _embed_all(embedder, [surfaces[keyword] for keyword in keywords], cache_path, log)
    position = {keyword: i for i, keyword in enumerate(keywords)}

    for start in range(0, len(pending), KEYWORD_BATCH):
        batch = pending[start : start + KEYWORD_BATCH]
        proposed = _propose(batch, keywords, position, vectors)
        stats.keywords_examined += len(batch)
        stats.pairs_proposed += len(proposed)
        todo = [pair for pair in proposed if not _has_pair(conn, pair)]
        stats.pairs_already_judged += len(proposed) - len(todo)
        verdicts = _judge_all(judge, [(surfaces[a], surfaces[b]) for a, b in todo])
        judged_at = datetime.now(UTC).isoformat(timespec="seconds")
        with conn:
            for (a, b), verdict in zip(todo, verdicts, strict=True):
                stats.pairs_judged += conn.execute(
                    "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_score, jev_model, "
                    "judged_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (a, b, verdict.score, verdict.model, judged_at),
                ).rowcount
        stats.batches_committed += 1
        log(
            f"{stats.keywords_examined:,}/{len(pending):,} keyword(s) examined, "
            f"{stats.pairs_judged:,} pair(s) judged"
        )
    return stats


def _surfaces(conn: sqlite3.Connection) -> dict[str, str]:
    """Each stored keyword and the text to embed and show Jev: its shortest
    surface form, shortest first then alphabetical so the choice is stable. A
    keyword no `keyword_forms` row maps back to is its own text."""
    chosen: dict[str, str] = {}
    for surface, keyword in conn.execute("SELECT surface, keyword FROM keyword_forms"):
        best = chosen.get(keyword)
        if best is None or (len(surface), surface) < (len(best), best):
            chosen[keyword] = surface
    return {
        row[0]: chosen.get(row[0], row[0])
        for row in conn.execute(
            "SELECT DISTINCT value FROM enrichment WHERE namespace = ? AND key = ?",
            (NAMESPACE, _KEY),
        )
    }


#: Texts embedded between cache saves, so a sweep killed during the first
#: full embed keeps most of what it finished.
_SAVE_EVERY = EMBED_BATCH * 20


def _embed_all(
    embedder: Embedder,
    texts: list[str],
    cache_path: Path | None,
    log: Callable[[str], None],
) -> _Vectors:
    """Unit-length vectors, one row per text, so a dot product is a cosine.
    Texts the cache holds are not sent to `embedder`."""
    known = _load_cache(cache_path) if cache_path else {}
    missing = [text for text in dict.fromkeys(texts) if text not in known]
    log(
        f"embedding {len(missing):,} of {len(texts):,} keyword(s); "
        f"{len(texts) - len(missing):,} cached"
    )
    for start in range(0, len(missing), EMBED_BATCH):
        chunk = missing[start : start + EMBED_BATCH]
        for text, vector in zip(chunk, embedder.embed(chunk), strict=True):
            known[text] = _unit(np.asarray(vector, dtype=np.float32))
        if cache_path and (start + EMBED_BATCH) % _SAVE_EVERY == 0:
            _save_cache(cache_path, known)
    matrix: _Vectors = np.stack([known[text] for text in texts])
    if cache_path and missing:
        _save_cache(cache_path, {text: known[text] for text in texts})
    return matrix


def _unit(vector: _Vectors) -> _Vectors:
    norm = float(np.linalg.norm(vector))
    unit: _Vectors = vector / norm if norm else vector
    return unit


def _load_cache(path: Path) -> dict[str, _Vectors]:
    """The cached vector per text, or nothing when the file is absent, unreadable
    or was made by another model."""
    try:
        with np.load(path, allow_pickle=False) as data:
            if str(data["model"]) != MODEL:
                return {}
            return dict(zip((str(t) for t in data["texts"]), data["vectors"], strict=True))
    except (OSError, ValueError, KeyError):
        return {}


def _save_cache(path: Path, vectors: dict[str, _Vectors]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("wb") as handle:
            np.savez(
                handle,
                model=MODEL,
                texts=np.array(list(vectors)),
                vectors=np.stack(list(vectors.values())) if vectors else np.empty((0, 0)),
            )
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _propose(
    batch: list[str], keywords: list[str], position: dict[str, int], vectors: _Vectors
) -> list[tuple[str, str]]:
    """The distinct pairs `batch` proposes, ordered, each with `a < b`."""
    rows = [position[keyword] for keyword in batch]
    sims = vectors[rows] @ vectors.T
    sims[np.arange(len(rows)), rows] = -np.inf
    k = min(NEIGHBOURS, len(keywords))
    nearest = np.argpartition(-sims, k - 1, axis=1)[:, :k]
    pairs: set[tuple[str, str]] = set()
    for row, keyword in enumerate(batch):
        for column in nearest[row]:
            if sims[row, column] >= MIN_COSINE:
                other = keywords[column]
                pairs.add((keyword, other) if keyword < other else (other, keyword))
    return sorted(pairs)


def _has_pair(conn: sqlite3.Connection, pair: tuple[str, str]) -> bool:
    found: Any = conn.execute(
        "SELECT 1 FROM keyword_pairs WHERE keyword_a = ? AND keyword_b = ?", pair
    ).fetchone()
    return found is not None


def _judge_all(judge: Judge, pairs: list[tuple[str, str]]) -> list[Verdict]:
    """Every pair's verdict, in order, or the first failure with nothing returned."""
    if not pairs:
        return []
    with ThreadPoolExecutor(JUDGE_WORKERS) as pool:
        futures = [pool.submit(judge.judge, a, b) for a, b in pairs]
        try:
            return [future.result() for future in futures]
        except BaseException:
            for future in futures:
                future.cancel()
            raise
