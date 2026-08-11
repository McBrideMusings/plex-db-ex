"""When a fetched row is due to be fetched again.

Every external source in this store caches the same way — write a timestamp
next to what was fetched, and skip a re-fetch while that timestamp is inside a
window. The rule does not vary by source, so it lives here rather than being
restated per sweep: TMDB keywords and edges (`tmdb_common.py`) and crowd-list
harvesting (`collections.py`) all read it from this one place.

`config.py` deliberately keeps its own `_DEFAULT_*_STALE_DAYS` literals rather
than importing `DEFAULT_STALE_DAYS` from here (issue #20). That keeps
`config.py` a leaf like `errors.py` and `schema.py`, importing no feature
module, and it lets an operator dial each source's window independently even
though they share a documented default.
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
