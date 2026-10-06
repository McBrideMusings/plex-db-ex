"""A Plex TVX decisions file: a person's accepts and rejects, waiting for the sweep.

Plex TVX never writes `plexdb.db` (ADR-0007, ADR-0017), so a decision made on one of
its review tabs goes into a JSON file beside its saved-queries file, and a sweep step
folds that file into the store. `merge_decisions.json` (the Merges tab) and
`role_decisions.json` (the Roles tab) share this shape: a list of entries, each a few
text fields naming what was decided, plus `decision` (`accepted`, `rejected` or
`cleared`) and `decided_at`. Entries apply in order, so the last one per key wins.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from .config import Config
from .errors import DecisionsFileError

__all__ = [
    "DECISIONS",
    "EXPLORE_SAVED_PATH_VAR",
    "SAVED_FILE",
    "DecisionsFile",
    "FoldStats",
    "decisions_dir",
]

DECISIONS = ("accepted", "rejected", "cleared")

#: Full path to the saved-queries file, set in the container to a dedicated
#: mount (`/explore-data`, its own bind mount in `[docker_run]`) rather than
#: etv-station's `/snapshot` directory — Plex TVX has no login, so anyone
#: who reaches the port could otherwise write into a directory another
#: consumer reads (plex-db-ex-oyg.3). Unset in dev, where `make_server` falls
#: back to a file beside the store.
EXPLORE_SAVED_PATH_VAR = "PLEXDB_EXPLORE_SAVED_PATH"

#: The saved-queries file's name. In dev it sits beside the store; in the
#: container `EXPLORE_SAVED_PATH_VAR` names its own dedicated mount instead.
SAVED_FILE = "explore-queries.json"


def decisions_dir(config: Config) -> Path:
    """Where Plex TVX's files sit: the saved queries, `merge_decisions.json` and
    `role_decisions.json`.

    The directory of `PLEXDB_EXPLORE_SAVED_PATH` when set (the container's own
    mount); otherwise beside the published snapshot when one is configured, as the
    scheduler's Plex TVX puts it, and beside the store otherwise, as `plexdb
    explore` does.
    """
    raw = os.environ.get(EXPLORE_SAVED_PATH_VAR, "").strip()
    if raw:
        return Path(raw).expanduser().parent
    return (config.snapshot_path or config.store_path).parent


@dataclass
class FoldStats:
    file_found: bool = False
    entries: int = 0
    #: Entries naming something the store has a row for, whether or not its decision changed.
    matched: int = 0
    #: Entries naming something with no row. Not an error: the file may name a row a
    #: restored older store never had.
    unmatched: int = 0


class DecisionsFile:
    """One decisions file, written the way `SavedQueries` writes its file.

    Replaced by rename so a reader never sees half of it, under a lock, and bounded
    because Plex TVX has no login. Only the last entry per key counts (the fold
    applies entries in order), so a write keeps one entry per key and the file's
    size is bounded by the number of rows the store holds.

    A subclass names its key fields (`KEY`), its error type, and what else an
    entry's key must satisfy (`key_problem`).
    """

    MAX_ENTRIES = 50_000
    MAX_TOTAL_BYTES = 8 << 20
    #: A keyword longer than this cannot be a stored value, so a write refuses it
    #: before looking it up.
    KEYWORD_MAX = 200

    #: The text fields naming what an entry decides, in key order.
    KEY: ClassVar[tuple[str, ...]]
    ERROR: ClassVar[type[DecisionsFileError]]
    #: What the file holds, for messages ("merge decisions").
    NOUN: ClassVar[str]

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @classmethod
    def key_problem(cls, key: tuple[str, ...]) -> str | None:
        """Why `key` cannot be an entry's key, or None."""
        return None

    @classmethod
    def parse(cls, path: Path, text: str) -> list[dict[str, str]]:
        """The file's entries, or `ERROR` if any breaks the documented shape."""
        try:
            raw = json.loads(text)
        except ValueError as err:
            raise cls.ERROR(f"{path} is not valid JSON: {err}") from None
        if not isinstance(raw, list):
            raise cls.ERROR(f"{path} does not hold a list of decisions")
        fields = (*cls.KEY, "decision", "decided_at")
        for index, entry in enumerate(raw):
            where = f"{path} entry {index}"
            if not isinstance(entry, dict) or not all(
                isinstance(entry.get(field), str) for field in fields
            ):
                raise cls.ERROR(f"{where} needs text {', '.join(fields[:-1])} and {fields[-1]}")
            if entry["decision"] not in DECISIONS:
                raise cls.ERROR(
                    f"{where} has decision {entry['decision']!r}, expected one of {DECISIONS}"
                )
            problem = cls.key_problem(tuple(entry[field] for field in cls.KEY))
            if problem:
                raise cls.ERROR(f"{where} {problem}")
            if entry["decision"] != "cleared":
                try:
                    datetime.fromisoformat(entry["decided_at"])
                except ValueError:
                    raise cls.ERROR(
                        f"{where} has decided_at {entry['decided_at']!r}, expected an ISO 8601 time"
                    ) from None
        entries: list[dict[str, str]] = raw
        return entries

    @classmethod
    def load(cls, path: Path) -> list[dict[str, str]] | None:
        """The entries in `path`, or None when there is no file."""
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as err:
            raise cls.ERROR(f"cannot read {path}: {err}") from None
        return cls.parse(path, text)

    @classmethod
    def keyword(cls, name: str, value: object) -> str:
        """`value` as a keyword a request may name, or `ValueError`."""
        if not isinstance(value, str) or not value or len(value) > cls.KEYWORD_MAX:
            raise ValueError(f"{name} must be text of 1 to {cls.KEYWORD_MAX} characters")
        return value

    @staticmethod
    def decision(value: object) -> str:
        """`value` as a decision a request may make, or `ValueError`."""
        if not isinstance(value, str) or value not in DECISIONS:
            raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
        return value

    def _key(self, entry: dict[str, str]) -> tuple[str, ...]:
        return tuple(entry[field] for field in self.KEY)

    def _write(self, body: str) -> None:
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(self._path)
        except OSError as err:
            raise self.ERROR(f"cannot write {self._path}: {err}") from None
        finally:
            tmp.unlink(missing_ok=True)

    def latest(self) -> dict[tuple[str, ...], dict[str, str]]:
        """The last entry for each key."""
        with self._lock:
            return {self._key(e): e for e in self.load(self._path) or []}

    def record(self, key: tuple[str, ...], decision: str) -> dict[str, str]:
        """Append one decision, dropping the key's earlier entries; return the entry.

        The caller has checked the fields and that the store has a row for `key`.
        """
        entry = dict(zip(self.KEY, key, strict=True))
        entry["decision"] = decision
        entry["decided_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            kept = [e for e in self.load(self._path) or [] if self._key(e) != key]
            kept.append(entry)
            if len(kept) > self.MAX_ENTRIES:
                raise ValueError(f"too many {self.NOUN} (max {self.MAX_ENTRIES})")
            body = json.dumps(kept, indent=2) + "\n"
            if len(body.encode()) > self.MAX_TOTAL_BYTES:
                raise ValueError(f"{self.NOUN} would exceed {self.MAX_TOTAL_BYTES} bytes")
            self._write(body)
        return entry
