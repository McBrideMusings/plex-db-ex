"""`plexdb repair-identities` — split identities that fused two unrelated
titles, and rewind the play cursor over what they cost (issue #23)."""

from __future__ import annotations

import argparse

from ..config import Config
from ..errors import ConfigError
from ..plex_client import LivePlexClient
from ..repair import fused_item_ids, kinds_by_rating_key, repair
from ..store import open_store

ORDER = 25


def _cmd_repair_identities(args: argparse.Namespace) -> int:
    config = Config.from_env()

    if not config.plex_url or not config.plex_token:
        raise ConfigError(
            "PLEX_URL and PLEX_TOKEN must be set in .env to repair identities — "
            "only Plex knows what media kind each rating key is, and that is what "
            "separates a fused identity from a legitimately merged one"
        )
    client = LivePlexClient(config.plex_url, config.plex_token)

    if args.dry_run:
        with open_store(config.store_path) as conn:
            fused = fused_item_ids(conn, kinds_by_rating_key(client))
        print(
            f"{len(fused)} identit(ies) cover Plex records of more than one media kind — "
            "each one is two unrelated titles fused into a single row"
        )
        for item_id in fused[:20]:
            print(f"  {item_id}")
        if len(fused) > 20:
            print(f"  ... and {len(fused) - 20} more")
        return 0

    with open_store(config.store_path) as conn:
        stats = repair(conn, client, config.source_roots)

    if not stats.fused_found:
        print("no fused identities found — nothing to repair")
        return 0

    print(
        f"deleted {stats.fused_found} fused identit(ies) and re-walked Plex: "
        f"{stats.walk.titles_written if stats.walk else 0} title(s) rewritten"
    )
    if stats.plays_repointed:
        print(
            f"{stats.plays_repointed} play(s) carried across by their own rating key — "
            "no history re-read needed for those"
        )
    if stats.plays_orphaned:
        print(
            f"{stats.plays_orphaned} play(s) could not be placed: their rating key is no "
            "longer in the library, so the title left Plex between the snapshot and the walk"
        )
    if stats.plays_dropped:
        print(
            f"{stats.plays_dropped} play(s) predate schema v5 and carry no rating key, so "
            f"nothing records which title they came from; the ingest cursor is rewound to "
            f"{stats.cursor_rewound_to} — run `plexdb ingest-plays` to read them back, then "
            "`plexdb enrich-tautulli-plays` if Tautulli is configured"
        )
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        "repair-identities",
        help="split identities that fused a movie and a TV show sharing a TMDB or TVDB "
        "number, and rewind the play cursor over what they cost",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list the fused identities and change nothing; still reads Plex",
    )
    parser.set_defaults(func=_cmd_repair_identities)
