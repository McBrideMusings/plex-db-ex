"""A `TMDbSource` built from the recorded fixtures in `fixtures/tmdb/`.

Not a test module itself (no `test_` prefix, so pytest never collects it) —
shared by `test_tmdb_client.py`, which drives `LiveTMDbClient` through
`httpx.MockTransport` against the recordings, and `test_enrich_tmdb.py`,
which drives `enrich_tmdb_keywords` against the in-memory fakes below.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plexdb.errors import TMDbError

FIXTURES = Path(__file__).parent / "fixtures" / "tmdb"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


@dataclass
class FakeTMDbSource:
    """A `TMDbSource` over an in-memory keyword map — no HTTP.

    `calls` records every `(tmdb_id, media_type)` pair asked of it, in the
    order asked, so a test can assert exactly what was (and wasn't) fetched.

    `fail_calls` holds 1-indexed call numbers that raise `TMDbError` instead
    of returning — `{1, 2, 4}` fails the first, second and fourth ask. Scripting
    failures by call number rather than by id is what lets a test lay out a
    run of failures and the success that breaks it.
    """

    keywords_by_id: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    fail_calls: set[int] = field(default_factory=set)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def keywords(self, tmdb_id: str, media_type: str) -> list[str]:
        self.calls.append((tmdb_id, media_type))
        if len(self.calls) in self.fail_calls:
            raise TMDbError(f"scripted failure on call {len(self.calls)} ({tmdb_id}, {media_type})")
        return list(self.keywords_by_id.get((tmdb_id, media_type), []))


@dataclass
class FailOnRepeatSource:
    """A `TMDbSource` that raises if the same `(tmdb_id, media_type)` pair is
    ever asked for twice.

    This is the caching-discipline test double issue #4 names explicitly:
    "a fake source that fails the test when asked twice for the same
    title" — a stronger proof than inspecting a stored timestamp after the
    fact, because it fails the instant the network call would have happened.
    """

    keywords_by_id: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def keywords(self, tmdb_id: str, media_type: str) -> list[str]:
        key = (tmdb_id, media_type)
        if key in self.calls:
            raise AssertionError(f"asked for {key} twice — the cache should have prevented this")
        self.calls.append(key)
        return list(self.keywords_by_id.get(key, []))
