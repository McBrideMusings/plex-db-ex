"""What a Gated Source is, and the one place its nine steps are written.

A Gated Source is an external source whose units carry a `fetched_at` and are
re-fetched only once stale — the rule CLAUDE.md states as "enrich once, keyed
by external id, with `fetched_at`". Four commands implement it today
(`enrich-tmdb-keywords`, `enrich-tmdb-edges`, `harvest-mdblist`,
`enrich-wikidata`).

The unit is not always a title: keywords and edges gate per title, MDBList per
list. `enrich-tautulli-plays` reads an external thing and is **not** a Gated Source
— it matches rows it has already seen rather than fetching per unit. Pulling
it in here would be inventing a framework, which is how this abstraction goes
wrong.

## Where the staleness window comes from

Each source reads `<NAME>_STALE_DAYS` — derived from its own `name`, so
`tmdb_keywords` reads `TMDB_KEYWORDS_STALE_DAYS` — falling back to the one
`DEFAULT_STALE_DAYS` in `staleness.py`. Adding a source therefore adds no
setting to `config.py`, which is what lets that module stay a leaf importing no
feature module (issue #20) while the default lives in exactly one place.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .config import Config
from .errors import ConfigError
from .staleness import DEFAULT_STALE_DAYS
from .store import open_store

__all__ = ["GatedSource"]


@dataclass(frozen=True)
class GatedSource:
    """One external source that caches what it fetched and skips what is fresh.

    Everything here is what actually varies between sources. The order the
    steps run in, the credential check (for a source that has one), the
    staleness resolution, the store handle and the two flags are the same for
    all of them and live in `run` and `register` below.
    """

    #: Stem of this source's staleness variable and the name a person sees in
    #: its messages, e.g. `tmdb_keywords` → `TMDB_KEYWORDS_STALE_DAYS`.
    name: str
    #: What one gated unit is, for the `--stale-days` help: a title for TMDB,
    #: a list for MDBList.
    unit: str
    #: Builds the live client: from the credential when the source declares
    #: one, from nothing when it does not. Written as a lambda in each source
    #: rather than the client class itself, so the name resolves from that
    #: module's globals on every call and a test can substitute a
    #: fixture-backed client with `monkeypatch.setattr(module, "LiveXClient", …)`.
    make_client: Callable[..., Any]
    #: `(conn, client, stale_days=…) -> stats`. Every source already has this
    #: shape, which is why this contract is small.
    #:
    #: Bound directly, unlike `make_client` — deliberately, not by oversight.
    #: The lambda there exists because tests really do replace the client; a
    #: test replacing the refresh function would be testing nothing. If one
    #: ever needs to, wrap it in a lambda for the same reason, because patching
    #: the module global alone would leave this field holding the original and
    #: the "faked" run would hit the live API.
    refresh: Callable[..., Any]
    #: Deletes everything this source owns, for `--rewipe`, and returns the
    #: lines describing what went. Each source's tables differ, so this stays
    #: a closure rather than one signature bent to fit three shapes.
    wipe: Callable[[sqlite3.Connection], Iterable[str]]
    #: Turns this source's own stats object into its summary lines. Nothing is
    #: forced into a shared result shape — a sweep only needs pass or fail.
    report: Callable[[Any], Iterable[str]]
    #: The environment variable carrying this source's credential, or `None`
    #: for a source that needs none (Wikidata answers anyone who sends a
    #: User-Agent). A keyless source's `make_client` takes no argument.
    credential: str | None = None
    #: What the credential is for, completing "…must be set in .env to <this>".
    #: Set exactly when `credential` is.
    credential_purpose: str | None = None

    def __post_init__(self) -> None:
        if (self.credential is None) != (self.credential_purpose is None):
            raise ValueError(f"{self.name}: credential and credential_purpose go together")

    @property
    def stale_days_var(self) -> str:
        return f"{self.name.upper()}_STALE_DAYS"

    def resolve_stale_days(self, override: int | None) -> int:
        """The flag wins, then `<NAME>_STALE_DAYS`, then the shared default.

        An unset *or empty* variable means "use the default" — an empty value
        is how a `.env` carries a placeholder, and treating it as zero would
        silently re-fetch everything on every run.
        """
        if override is not None:
            return override
        raw = os.environ.get(self.stale_days_var, "").strip()
        if not raw:
            return DEFAULT_STALE_DAYS
        try:
            return int(raw)
        except ValueError as err:
            raise ConfigError(
                f"{self.stale_days_var} must be a whole number of days, got {raw!r}"
            ) from err

    def run(self, args: argparse.Namespace) -> int:
        """The nine steps every Gated Source command used to write out itself."""
        config = Config.from_env()
        if self.credential is None:
            client = self.make_client()
        else:
            credential = os.environ.get(self.credential, "").strip()
            if not credential:
                raise ConfigError(
                    f"{self.credential} must be set in .env to {self.credential_purpose}"
                )
            client = self.make_client(credential)
        stale_days = self.resolve_stale_days(args.stale_days)
        with open_store(config.store_path) as conn:
            if args.rewipe:
                for line in self.wipe(conn):
                    print(line)
            stats = self.refresh(conn, client, stale_days=stale_days)
        for line in self.report(stats):
            print(line)
        return 0

    def register(
        self,
        sub: argparse._SubParsersAction[argparse.ArgumentParser],
        name: str,
        *,
        help: str,  # noqa: A002 — argparse's own parameter name
        rewipe_help: str,
    ) -> None:
        """Add this source's subparser. The two flags and the shape of their
        help are identical across sources; only the nouns differ."""
        parser = sub.add_parser(name, help=help)
        parser.add_argument(
            "--stale-days",
            type=int,
            default=None,
            metavar="N",
            help=f"re-fetch a {self.unit} whose stored {self.name} data is older than this "
            f"many days; default: {self.stale_days_var}, or {DEFAULT_STALE_DAYS} if that is unset",
        )
        parser.add_argument("--rewipe", action="store_true", help=rewipe_help)
        parser.set_defaults(func=self.run)
