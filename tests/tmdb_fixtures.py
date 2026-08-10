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
    """A `TMDbSource` over in-memory keyword/recommendations/similar maps — no
    HTTP.

    `calls`, `recommendation_calls`, and `similar_calls` each record every
    `(tmdb_id, media_type)` pair asked of that one method, in the order
    asked, so a test can assert exactly what was (and wasn't) fetched.
    `fail_calls`/`fail_recommendation_calls`/`fail_similar_calls` hold
    1-indexed call numbers — scoped to their own method's call list — that
    raise `TMDbError` instead of returning; `{1, 2, 4}` fails the first,
    second and fourth ask *of that method*. Scripting failures by call
    number rather than by id is what lets a test lay out a run of failures
    and the success that breaks it.
    """

    keywords_by_id: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    fail_calls: set[int] = field(default_factory=set)
    calls: list[tuple[str, str]] = field(default_factory=list)

    recommendations_by_id: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    fail_recommendation_calls: set[int] = field(default_factory=set)
    recommendation_calls: list[tuple[str, str]] = field(default_factory=list)

    similar_by_id: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    fail_similar_calls: set[int] = field(default_factory=set)
    similar_calls: list[tuple[str, str]] = field(default_factory=list)

    def keywords(self, tmdb_id: str, media_type: str) -> list[str]:
        self.calls.append((tmdb_id, media_type))
        if len(self.calls) in self.fail_calls:
            raise TMDbError(f"scripted failure on call {len(self.calls)} ({tmdb_id}, {media_type})")
        return list(self.keywords_by_id.get((tmdb_id, media_type), []))

    def recommendations(self, tmdb_id: str, media_type: str) -> list[str]:
        self.recommendation_calls.append((tmdb_id, media_type))
        if len(self.recommendation_calls) in self.fail_recommendation_calls:
            raise TMDbError(
                f"scripted failure on recommendations call {len(self.recommendation_calls)} "
                f"({tmdb_id}, {media_type})"
            )
        return list(self.recommendations_by_id.get((tmdb_id, media_type), []))

    def similar(self, tmdb_id: str, media_type: str) -> list[str]:
        self.similar_calls.append((tmdb_id, media_type))
        if len(self.similar_calls) in self.fail_similar_calls:
            raise TMDbError(
                f"scripted failure on similar call {len(self.similar_calls)} "
                f"({tmdb_id}, {media_type})"
            )
        return list(self.similar_by_id.get((tmdb_id, media_type), []))


@dataclass
class FailOnRepeatSource:
    """A `TMDbSource` that raises if the same `(tmdb_id, media_type)` pair is
    ever asked for twice of the same method.

    This is the caching-discipline test double issue #4 names explicitly:
    "a fake source that fails the test when asked twice for the same
    title" — a stronger proof than inspecting a stored timestamp after the
    fact, because it fails the instant the network call would have happened.
    Each method tracks its own repeats independently, since a title's
    recommendations cursor and similar cursor are refreshed on their own
    schedule.
    """

    keywords_by_id: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)

    recommendations_by_id: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    recommendation_calls: list[tuple[str, str]] = field(default_factory=list)

    similar_by_id: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    similar_calls: list[tuple[str, str]] = field(default_factory=list)

    def keywords(self, tmdb_id: str, media_type: str) -> list[str]:
        key = (tmdb_id, media_type)
        if key in self.calls:
            raise AssertionError(f"asked for {key} twice — the cache should have prevented this")
        self.calls.append(key)
        return list(self.keywords_by_id.get(key, []))

    def recommendations(self, tmdb_id: str, media_type: str) -> list[str]:
        key = (tmdb_id, media_type)
        if key in self.recommendation_calls:
            raise AssertionError(
                f"asked for recommendations({key}) twice — the cache should have prevented this"
            )
        self.recommendation_calls.append(key)
        return list(self.recommendations_by_id.get(key, []))

    def similar(self, tmdb_id: str, media_type: str) -> list[str]:
        key = (tmdb_id, media_type)
        if key in self.similar_calls:
            raise AssertionError(
                f"asked for similar({key}) twice — the cache should have prevented this"
            )
        self.similar_calls.append(key)
        return list(self.similar_by_id.get(key, []))
