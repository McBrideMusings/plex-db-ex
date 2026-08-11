"""`plexdb ingest-plays` — ingest Plex watch history into plays."""

from __future__ import annotations

import argparse

from ..config import Config
from ..errors import ConfigError
from ..plays import ingest_plays
from ..plex_client import LivePlexClient
from ..store import open_store
from ..sweep import Step

NAME = "ingest-plays"
#: Ahead of the enrichment block on purpose: plays are the data no re-scan
#: reproduces, TMDB is re-fetchable, and a sweep killed partway should already
#: have banked the plays.
ORDER = 30
SWEEP = Step.REQUIRED


def _cmd_ingest_plays(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    if not config.plex_url or not config.plex_token:
        raise ConfigError("PLEX_URL and PLEX_TOKEN must be set in .env to ingest watch history")
    client = LivePlexClient(config.plex_url, config.plex_token)
    with open_store(config.store_path) as conn:
        stats = ingest_plays(conn, client)
    print(
        f"ingested {stats.plays_written} play(s) from {stats.events_seen} event(s) seen, "
        f"{stats.already_recorded} already recorded"
    )
    if stats.unresolved_rating_key:
        print(
            f"{stats.unresolved_rating_key} event(s) had a rating key not in the walk's map, "
            "skipped — run `plexdb walk` to catch up"
        )
    if stats.unresolved_device:
        print(
            f"{stats.unresolved_device} event(s) had a device id not in Plex's device list; "
            "recorded with no client identifier or platform"
        )
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    ingest_plays_parser = sub.add_parser(
        NAME,
        help="ingest Plex watch history into plays; safe to re-run",
    )
    ingest_plays_parser.set_defaults(func=_cmd_ingest_plays)
