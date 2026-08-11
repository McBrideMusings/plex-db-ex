"""Rules shared by the two TMDB sweeps: media-type mapping and the
consecutive-failure threshold.

`enrich_tmdb.py` and `tmdb_edges.py` both walk `items` looking for TMDB-linked
movies and shows, both cache against a staleness window, and both give up on
a run after the same number of consecutive failures. Issue #22: those pieces
used to be copied verbatim into each sweep, so the risk was a title shape one
copy of `media_type_for` handled and the other did not — the keyword sweep
finds the title, the edges sweep silently skips it, and nothing raises. This
module is the one place both sweeps read these rules from, so drift like that
can no longer happen.

**Staleness is not here.** It moved to `staleness.py` when crowd-list
harvesting became a third source that caches the same way (issue #34) —
nothing about "has this timestamp aged out" is TMDB-specific, and a
non-TMDB sweep importing a module named for TMDB is how a rule starts getting
copied instead of shared. What is left here genuinely is TMDB's.
"""

from __future__ import annotations

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
