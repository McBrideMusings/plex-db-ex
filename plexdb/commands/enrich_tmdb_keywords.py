"""`plexdb enrich-tmdb-keywords` — fetch TMDB keywords for walked movies and
shows into the `keywords` namespace, tagged `source='tmdb'` (ADR-0016)."""

from __future__ import annotations

import argparse
import sqlite3

from ..enrich_tmdb import NAMESPACE as TMDB_KEYWORDS_NAMESPACE
from ..enrich_tmdb import EnrichStats, enrich_tmdb_keywords, wipe_namespace
from ..sources import GatedSource
from ..sweep import Step
from ..tmdb_client import LiveTMDbClient

NAME = "enrich-tmdb-keywords"
ORDER = 50
#: TMDB is an optional source behind an adapter (ADR-0004). Staleness gating
#: means a skipped pass costs nothing but freshness — the next run picks up
#: exactly the titles this one missed.
SWEEP = Step.BEST_EFFORT


def _wipe(conn: sqlite3.Connection) -> list[str]:
    return [f"wiped {wipe_namespace(conn)} row(s) from the {TMDB_KEYWORDS_NAMESPACE} namespace"]


def _report(stats: EnrichStats) -> list[str]:
    lines = [
        f"tmdb keywords: {stats.titles_seen} title(s) seen, "
        f"{stats.titles_fetched} fetched, {stats.titles_cached} already cached, "
        f"{stats.titles_skipped_no_tmdb_id} skipped (no tmdb id), "
        f"{stats.titles_failed} failed, "
        f"{stats.keywords_written} keyword(s) written"
    ]
    if stats.titles_failed:
        lines.append(
            f"{stats.titles_failed} title(s) failed and were not cached — re-run to retry them"
        )
    return lines


SOURCE = GatedSource(
    name="tmdb_keywords",
    unit="title",
    credential="TMDB_API_KEY",
    credential_purpose="fetch TMDB keywords",
    # A lambda, not the class itself: it resolves `LiveTMDbClient` from this
    # module's globals on every call, which is what lets a test substitute a
    # fixture-backed client with `monkeypatch.setattr(module, "LiveTMDbClient", …)`.
    # Binding the class here directly would capture it at import and silently
    # ignore the patch — the test would hit the live API.
    make_client=lambda api_key: LiveTMDbClient(api_key),
    refresh=enrich_tmdb_keywords,
    wipe=_wipe,
    report=_report,
)


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    SOURCE.register(
        sub,
        NAME,
        help="fetch TMDB keywords for walked movies/shows into the keywords "
        "namespace (source='tmdb'); a fresh row is never re-fetched",
        rewipe_help="delete every source='tmdb' keyword row before the sweep, forcing a "
        "full re-fetch; another source's keywords and every other namespace are untouched",
    )
