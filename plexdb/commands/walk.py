"""`plexdb walk` — walk the Plex library into items, external_ids, and the
rating-key map."""

from __future__ import annotations

import argparse

from ..config import Config
from ..errors import ConfigError
from ..plex_client import LivePlexClient
from ..store import open_store
from ..sweep import Step
from ..walk import walk_all

NAME = "walk"
ORDER = 20
#: Plex is the one required dependency (ADR-0004). A sweep that could not read
#: the library must not publish a snapshot built on what it managed to get.
SWEEP = Step.REQUIRED
#: How many kept identities to name per resolution path before summarising the
#: rest as a count.
_KEPT_SHOWN = 20


def _cmd_walk(args: argparse.Namespace) -> int:
    config = Config.from_env()
    if not config.plex_url or not config.plex_token:
        raise ConfigError("PLEX_URL and PLEX_TOKEN must be set in .env to walk the library")
    # An empty root list is silently wrong rather than harmlessly absent: a
    # GUID-less title's item_id becomes a hash of the mount point, so it joins
    # with nothing in any other store (issue #24). Refuse rather than write it.
    if not config.source_roots:
        raise ConfigError(
            "PLEX_SOURCE_ROOTS must be set in .env to walk the library. "
            "It is the mount root to strip from a Plex path before hashing it, and "
            "a title with no external GUID gets its item_id from that hash — so with "
            "no root set, the id encodes where the disk is mounted and matches no "
            "other store. Set it to the directory the library sections live under "
            "(the section paths reported by Plex all sit beneath it)."
        )
    client = LivePlexClient(config.plex_url, config.plex_token)
    with open_store(config.store_path) as conn:
        stats = walk_all(conn, client, config.source_roots, section_key=args.section)
    print(
        f"walked {stats.sections_walked} section(s): "
        f"{stats.titles_seen} title(s) seen, {stats.titles_written} written, "
        f"{stats.fallback_to_path} fell back to a path-derived id"
    )
    for path, found_by in (("rating_key", "rating key"), ("external_id", "external id")):
        kept = stats.kept_by(path)
        if not kept:
            continue
        print(
            f"{len(kept)} title(s) would have derived a different item_id this walk; "
            f"kept their existing one (found by {found_by}) rather than forking a new row"
        )
        # Named, not just counted (issue #56). Each line is what someone needs
        # to decide whether the keep was right: a title Plex stopped reporting
        # an id for keeps its identity correctly, whereas two different titles
        # Plex handed one id land on one row and lose one of them. The cap
        # keeps a library-wide event — a mount path that moved, issue #55,
        # printed 1521 of these — from burying the rest of the summary.
        for entry in kept[:_KEPT_SHOWN]:
            print(f"    {entry.label} [rating key {entry.rating_key}]")
            print(f"        kept {entry.kept_id}, would have derived {entry.derived_id}")
        if len(kept) > _KEPT_SHOWN:
            print(f"    ... and {len(kept) - _KEPT_SHOWN} more")
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    walk_parser = sub.add_parser(
        NAME,
        help="walk the Plex library into items, external_ids, and the rating-key map; "
        "safe to re-run",
    )
    walk_parser.add_argument(
        "--section",
        metavar="KEY",
        default=None,
        help="walk only this section key; default: every movie- and show-shaped section",
    )
    walk_parser.set_defaults(func=_cmd_walk)
