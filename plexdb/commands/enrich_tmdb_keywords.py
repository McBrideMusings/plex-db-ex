"""`plexdb enrich-tmdb-keywords` — fetch TMDB keywords for walked movies and
shows into the `tmdb_keywords` namespace."""

from __future__ import annotations

import argparse

from ..config import Config
from ..enrich_tmdb import NAMESPACE as TMDB_KEYWORDS_NAMESPACE
from ..enrich_tmdb import enrich_tmdb_keywords, wipe_namespace
from ..errors import ConfigError
from ..store import open_store
from ..sweep import Step
from ..tmdb_client import LiveTMDbClient

NAME = "enrich-tmdb-keywords"
ORDER = 50
#: TMDB is an optional source behind an adapter (ADR-0004). Staleness gating
#: means a skipped pass costs nothing but freshness — the next run picks up
#: exactly the titles this one missed.
SWEEP = Step.BEST_EFFORT


def _cmd_enrich_tmdb_keywords(args: argparse.Namespace) -> int:
    config = Config.from_env()
    if not config.tmdb_api_key:
        raise ConfigError("TMDB_API_KEY must be set in .env to fetch TMDB keywords")
    stale_days = args.stale_days if args.stale_days is not None else config.tmdb_keywords_stale_days
    client = LiveTMDbClient(config.tmdb_api_key)
    with open_store(config.store_path) as conn:
        if args.rewipe:
            removed = wipe_namespace(conn)
            print(f"wiped {removed} row(s) from the {TMDB_KEYWORDS_NAMESPACE} namespace")
        stats = enrich_tmdb_keywords(conn, client, stale_days=stale_days)
    print(
        f"tmdb keywords: {stats.titles_seen} title(s) seen, "
        f"{stats.titles_fetched} fetched, {stats.titles_cached} already cached, "
        f"{stats.titles_skipped_no_tmdb_id} skipped (no tmdb id), "
        f"{stats.titles_failed} failed, "
        f"{stats.keywords_written} keyword(s) written"
    )
    if stats.titles_failed:
        print(f"{stats.titles_failed} title(s) failed and were not cached — re-run to retry them")
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    enrich_tmdb_parser = sub.add_parser(
        NAME,
        help="fetch TMDB keywords for walked movies/shows into the tmdb_keywords "
        "namespace; a fresh row is never re-fetched",
    )
    enrich_tmdb_parser.add_argument(
        "--stale-days",
        type=int,
        default=None,
        metavar="N",
        help="re-fetch a title whose tmdb_keywords row is older than this many days; "
        "default: TMDB_KEYWORDS_STALE_DAYS, or 45 if that is unset",
    )
    enrich_tmdb_parser.add_argument(
        "--rewipe",
        action="store_true",
        help="delete every tmdb_keywords row before the sweep, forcing a full re-fetch; "
        "other namespaces are untouched",
    )
    enrich_tmdb_parser.set_defaults(func=_cmd_enrich_tmdb_keywords)
