"""Rules shared by the two TMDB sweeps: media-type mapping, staleness, and the
consecutive-failure threshold.

`enrich_tmdb.py` and `tmdb_edges.py` both walk `items` looking for TMDB-linked
movies and shows, both cache against a staleness window, and both give up on
a run after the same number of consecutive failures. Issue #22: those four
pieces used to be copied verbatim into each sweep, so the risk was a title
shape one copy of `media_type_for` handled and the other did not — the
keyword sweep finds the title, the edges sweep silently skips it, and nothing
raises. This module is the one place both sweeps read these rules from, so
drift like that can no longer happen.

`config.py` deliberately keeps its own `_DEFAULT_TMDB_*_STALE_DAYS` literals
rather than importing `DEFAULT_STALE_DAYS` from here (issue #20) — that keeps
`config.py` a leaf like `errors.py` and `schema.py`, importing no feature
module. This module is for the two sweeps only.
"""

from __future__ import annotations

from datetime import datetime

#: Documented default: mid-range of the 30-60 day window `docs/schema.md`
#: sets for every external source's enrichment.
DEFAULT_STALE_DAYS = 45

#: Consecutive TMDB failures before a sweep gives up on the rest of the
#: library. One flaky title fails alone; a revoked key or a rate limit fails
#: in a run, and grinding through the whole library against a dead key would
#: skip every title left while still reporting a clean-looking summary.
MAX_CONSECUTIVE_FAILURES = 3


def media_type_for(item_type: str) -> str | None:
    """`items.type` -> the TMDB path segment, or `None` for a type neither
    sweep enriches. TMDB's `/keywords`, `/recommendations`, and `/similar`
    endpoints all exist only for movies (`movie`) and shows (`tv`) — an
    episode, or any other Plex section type, is skipped the same way an
    unrecognised section type is skipped in `walk.py`: silently, never as an
    error."""
    if item_type == "movie":
        return "movie"
    if item_type == "show":
        return "tv"
    return None


def is_stale(fetched_at: str, cutoff: datetime) -> bool:
    """True when an `enrichment.fetched_at` timestamp predates `cutoff`, so the
    title is due a re-fetch. Both sweeps check this before calling TMDB, never
    after inspecting what came back."""
    return datetime.fromisoformat(fetched_at) < cutoff
