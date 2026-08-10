"""`plexdb latent-users` — cluster shared accounts' plays into latent users by
fingerprint, and report taste divergence between the clusters (issue #10).

Read-only: this command opens the store `open_readonly` and never writes.
Nothing it produces is persisted — no latent-user table exists, and the call
on whether a clustering is good enough to build on is the human's, made on
the report this prints, not by this command.
"""

from __future__ import annotations

import argparse

from ..clusters import render_report
from ..config import Config
from ..store import open_readonly

#: After ingest-plays (50) and enrich-tautulli-plays (55), whose output this
#: reads, and before reconcile-etv (60) — this is a report, not a pipeline
#: step, so its position among the enrichment commands is only a reading
#: convenience.
ORDER = 57


def _cmd_latent_users(args: argparse.Namespace) -> int:
    config = Config.from_env()
    with open_readonly(config.store_path) as conn:
        report = render_report(conn, args.account or None)
    print(report, end="")
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        "latent-users",
        help=(
            "cluster shared accounts' plays into latent users by fingerprint and report "
            "taste divergence between clusters; read-only, persists nothing"
        ),
    )
    parser.add_argument(
        "--account",
        type=int,
        action="append",
        metavar="PLEX_ACCOUNT_ID",
        help="cluster only this plex_account_id; repeatable. Default: every account with any plays",
    )
    parser.set_defaults(func=_cmd_latent_users)
