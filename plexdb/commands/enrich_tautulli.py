"""`plexdb enrich-tautulli-plays` — enrich `plays` with the fields only
Tautulli can supply: IP, completion percentage, and paused time (issue #9)."""

from __future__ import annotations

import argparse

from ..config import Config
from ..errors import ConfigError
from ..plays import match_tautulli_history
from ..store import open_store
from ..tautulli_client import LiveTautulliClient

#: Between ingest-plays (50), whose output this enriches, and reconcile-etv
#: (60).
ORDER = 55


def _cmd_enrich_tautulli_plays(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    if not config.tautulli_url or not config.tautulli_api_key:
        raise ConfigError(
            "TAUTULLI_URL and TAUTULLI_API_KEY must be set in .env to enrich plays from Tautulli"
        )
    client = LiveTautulliClient(config.tautulli_url, config.tautulli_api_key)
    with open_store(config.store_path) as conn:
        stats = match_tautulli_history(conn, client)
    print(
        f"matched {stats.rows_matched} Tautulli row(s) to plays "
        f"({stats.rows_seen} row(s) seen, {stats.rows_in_progress} in-progress skipped, "
        f"{stats.rows_already_matched} already matched)"
    )
    print(
        f"{stats.rows_unmatched} Tautulli row(s) found no matching play; "
        f"{stats.plays_without_tautulli_data} play(s) still carry no Tautulli data"
    )
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        "enrich-tautulli-plays",
        help=(
            "enrich plays with Tautulli's IP, completion percentage, and paused time; "
            "safe to re-run"
        ),
    )
    parser.set_defaults(func=_cmd_enrich_tautulli_plays)
