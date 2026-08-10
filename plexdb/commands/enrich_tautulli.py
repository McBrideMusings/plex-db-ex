"""`plexdb enrich-tautulli-plays` — enrich `plays` with the fields only
Tautulli can supply: IP, completion percentage, and paused time (issue #9).

Also resolves the server owner's account id across systems (issue #26):
Plex's history stores the owner under the local account id `1`, while
Tautulli reports the same person under their plex.tv id. Both `/accounts`
(Plex) and `get_users` (Tautulli) are fetched every run and joined on
account name — Plex is the store's one required dependency (ADR-0004), so
this adds no new external dependency, just a second live call alongside
`get_history`.
"""

from __future__ import annotations

import argparse

from ..config import Config
from ..errors import ConfigError
from ..plays import build_account_id_map, match_tautulli_history
from ..plex_client import LivePlexClient
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
    if not config.plex_url or not config.plex_token:
        raise ConfigError(
            "PLEX_URL and PLEX_TOKEN must be set in .env to resolve account ids against Plex"
        )
    plex_client = LivePlexClient(config.plex_url, config.plex_token)
    tautulli_client = LiveTautulliClient(config.tautulli_url, config.tautulli_api_key)

    account_id_map = build_account_id_map(plex_client.accounts(), tautulli_client.users())
    if account_id_map.tautulli_only_names:
        print(
            "Tautulli reports account name(s) with no matching Plex account: "
            + ", ".join(account_id_map.tautulli_only_names)
        )
    if account_id_map.plex_only_names:
        print(
            "Plex reports account name(s) with no matching Tautulli user: "
            + ", ".join(account_id_map.plex_only_names)
        )

    with open_store(config.store_path) as conn:
        stats = match_tautulli_history(conn, tautulli_client, account_id_map)
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
