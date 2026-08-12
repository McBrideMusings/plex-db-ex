"""`plexdb latent-users` — cluster shared accounts' plays into latent users by
fingerprint, and report taste divergence between the clusters (issue #10).
Every other account is reported as one named person, not clustered
(issue #27) — see `plexdb.clusters` for why.

Read-only against the store: opens it `open_readonly` and never writes.
Nothing it produces is persisted — no latent-user table exists, and the call
on whether a clustering is good enough to build on is the human's, made on
the report this prints, not by this command. It does reach Plex live (the
store's one required dependency, ADR-0004) to name each account in the
report — the same `/accounts` endpoint `enrich-tautulli-plays` already
reads.
"""

from __future__ import annotations

import argparse

from ..clusters import render_report
from ..config import Config
from ..errors import ConfigError
from ..plex_client import LivePlexClient, valid_accounts
from ..store import open_readonly

#: After ingest-plays (30) and enrich-tautulli-plays (35), whose output this
#: reads — this is a report, not a pipeline step, so its position among the
#: enrichment commands is only a reading convenience.
NAME = "latent-users"
ORDER = 70
# No SWEEP: read-only, persists nothing. A report nobody reads at 3am is not
# worth the Plex calls it makes.


def _cmd_latent_users(args: argparse.Namespace) -> int:
    config = Config.from_env()
    if not config.plex_url or not config.plex_token:
        raise ConfigError(
            "PLEX_URL and PLEX_TOKEN must be set in .env to name accounts in the report"
        )
    plex_client = LivePlexClient(config.plex_url, config.plex_token)
    account_names = {
        account["id"]: account["name"] for account in valid_accounts(plex_client.accounts())
    }
    with open_readonly(config.store_path) as conn:
        report = render_report(
            conn,
            args.account or None,
            shared_account_ids=config.shared_account_ids,
            account_names=account_names,
        )
    print(report, end="")
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
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
