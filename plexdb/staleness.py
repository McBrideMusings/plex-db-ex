"""When a fetched row is due to be fetched again.

Every external source in this store caches the same way — write a timestamp
next to what was fetched, and skip a re-fetch while that timestamp is inside a
window. The rule does not vary by source, so it lives here rather than being
restated per sweep: TMDB keywords and edges (`tmdb_common.py`) and crowd-list
harvesting (`collections.py`) all read it from this one place.

`config.py` holds no staleness setting at all, and this is the only copy of the
default. It used to keep three `_DEFAULT_*_STALE_DAYS` literals of its own so
it could stay a leaf importing no feature module (issue #20) while each source
kept an independently dialable window. `sources.py` gets both without the
duplication: a Gated Source derives `<NAME>_STALE_DAYS` from its own name and
reads it there, so the windows stay independent, `config.py` stays a leaf, and
the number 45 is written once.
"""

from __future__ import annotations

from datetime import datetime

#: Documented default: mid-range of the 30-60 day window `docs/schema.md`
#: sets for every external source's enrichment.
DEFAULT_STALE_DAYS = 45


def is_stale(fetched_at: str, cutoff: datetime) -> bool:
    """True when a stored timestamp predates `cutoff`, so the row is due a
    re-fetch. Every sweep checks this before calling the source, never after
    inspecting what came back."""
    return datetime.fromisoformat(fetched_at) < cutoff
