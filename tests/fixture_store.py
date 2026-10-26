"""Fixture stores, built by the writer's own migrate.

The store's schema is defined once, in `crates/plexdb-schema`, and `plexdb-rs
migrate` is what applies it. A test that needs a store runs that binary, never a
copy of its DDL, so a test can only pass against the tables the writer really
creates. `admin test` builds the binary and names it in `PLEXDB_RS_BIN`
(`tools/with-rs.sh`).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "FoldRefused",
    "Folded",
    "Refreshed",
    "fold",
    "init",
    "publish",
    "refresh_map",
    "refresh_tagnetwork",
]


def _binary() -> str:
    named = os.environ.get("PLEXDB_RS_BIN", "").strip() or "plexdb-rs"
    found = shutil.which(named)
    if found is None:
        raise RuntimeError(
            f"cannot build a fixture store: {named!r} not found. Run the tests with "
            "`admin test`, which builds plexdb-rs and sets PLEXDB_RS_BIN"
        )
    return found


def init(path: Path) -> None:
    """Create a store at `path` at the current schema, or bring an existing one
    up to it. Backups of an older store land in `backups/` beside it."""
    env = dict(os.environ, PLEXDB_PATH=str(path), PLEXDB_BACKUP_DIR=str(path.parent / "backups"))
    env.pop("PLEXDB_HELD_STORE", None)
    done = subprocess.run(
        [_binary(), "migrate"], env=env, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise RuntimeError(f"plexdb-rs migrate failed for {path}: {done.stderr.strip()}")


@dataclass(frozen=True)
class Folded:
    """What a `fold-*-decisions` step reported: the numbers in its one-line report."""

    file_found: bool
    entries: int
    matched: int
    unmatched: int


class FoldRefused(RuntimeError):
    """The step exited non-zero; the message is its `error:` line."""


def fold(step: str, store: Path, path: Path) -> Folded:
    """Run the Rust `fold-*-decisions` step `step` on `store`, reading the decisions
    file `path`, as the sweep would. The fold is Rust-only, so a test that needs its
    effect runs the binary instead of a copy of it."""
    env = dict(
        os.environ,
        PLEXDB_PATH=str(store),
        PLEXDB_EXPLORE_SAVED_PATH=str(path.parent / "explore-queries.json"),
    )
    env.pop("PLEXDB_HELD_STORE", None)
    done = subprocess.run([_binary(), step], env=env, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise FoldRefused(done.stderr.strip().removeprefix("error: "))
    out = done.stdout
    if ": no file at " in out:
        return Folded(False, 0, 0, 0)
    entries, matched, unmatched = _numbers(out)
    return Folded(True, entries, matched, unmatched)


def _numbers(report: str) -> tuple[int, int, int]:
    """`<what>: N entr(ies) in <path>, M <matched>, K <unmatched>` -> (N, M, K)."""
    head, _, rest = report.partition(" in ")
    entries = int(re.search(r"([\d,]+) entr", head).group(1).replace(",", ""))  # type: ignore[union-attr]
    counts = re.findall(r", ([\d,]+) ", rest)
    return entries, int(counts[0].replace(",", "")), int(counts[1].replace(",", ""))


@dataclass(frozen=True)
class Refreshed:
    """One kind's line from `refresh-map` or `refresh-tagnetwork`."""

    kind: str
    #: False when the stored view already matched the keyword rows.
    redrawn: bool
    #: Titles placed on the map, or nodes on the network.
    count: int


_REFRESHED = re.compile(r"^(?:title map|tag network): (\w+): (drew|current,) ([\d,]+) ")


def _step(step: str, store: Path, extra: dict[str, str] | None = None) -> str:
    env = dict(os.environ, PLEXDB_PATH=str(store)) | (extra or {})
    env.pop("PLEXDB_HELD_STORE", None)
    done = subprocess.run([_binary(), step], env=env, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"plexdb-rs {step} failed for {store}: {done.stderr.strip()}")
    return done.stdout


def _refreshed(step: str, store: Path) -> list[Refreshed]:
    found = []
    for line in _step(step, store).splitlines():
        match = _REFRESHED.match(line)
        if match is not None:
            kind, verb, count = match.groups()
            found.append(Refreshed(kind, verb == "drew", int(count.replace(",", ""))))
    return found


def refresh_map(store: Path) -> list[Refreshed]:
    """Run the Rust `refresh-map` step on `store`: the stored Title map, as the sweep draws it."""
    return _refreshed("refresh-map", store)


def refresh_tagnetwork(store: Path) -> list[Refreshed]:
    """Run the Rust `refresh-tagnetwork` step on `store`."""
    return _refreshed("refresh-tagnetwork", store)


def publish(store: Path, snapshot: Path) -> None:
    """Run the Rust `publish` step: a snapshot of `store` at `snapshot`."""
    _step("publish", store, {"PLEXDB_SNAPSHOT_PATH": str(snapshot)})
