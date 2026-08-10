"""Configuration, read from the environment.

Every connection detail lives in `.env`, which is gitignored. Nothing here
carries a real default for a URL, a token, or a host — a committed fallback
re-leaks the exact value the file exists to hide.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .errors import ConfigError

__all__ = ["DEFAULT_STORE_PATH", "Config", "ConfigError"]

#: Where the store lands when nothing says otherwise. A path is not a secret,
#: so unlike a URL or a token this one may carry a default.
DEFAULT_STORE_PATH = "./data/plexdb.db"

#: Documented default: mid-range of the 30-60 day window `docs/schema.md` sets
#: for every external source's enrichment. Deliberately duplicated from
#: `enrich_tmdb.DEFAULT_STALE_DAYS` rather than imported — `config.py` is a
#: leaf like `errors.py` and `schema.py`, and imports no feature module.
_DEFAULT_TMDB_KEYWORDS_STALE_DAYS = 45

#: Same documented default, duplicated for the same reason as the keywords
#: constant above — a separate knob from `tmdb_keywords_stale_days` so an
#: operator can dial edges and keywords staleness independently even though
#: both come from TMDB.
_DEFAULT_TMDB_EDGES_STALE_DAYS = 45


def _stale_days_from_env(var: str, default: int) -> int:
    """Read one whole-number staleness knob, falling back to its documented
    default when the variable is unset or empty.

    Shared by every `*_STALE_DAYS` knob so a second one cannot drift into a
    different rejection message or a silent `0` for a typo — the variable name
    is the only thing that differs between them.
    """
    raw = os.environ.get(var, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as err:
        raise ConfigError(f"{var} must be a whole number of days, got {raw!r}") from err


@dataclass(frozen=True)
class Config:
    """Everything the store needs to know about its environment."""

    store_path: Path
    #: Where `plexdb publish` writes the read-only snapshot consumers open
    #: (ADR-0007). `None` when unset — unlike `store_path`, this carries no
    #: default that points anywhere real, so a consumer can never be pointed
    #: at a path nobody chose.
    snapshot_path: Path | None
    #: The Plex server `plexdb walk` reads from (ADR-0005). Empty when unset
    #: — `init` and `publish` don't need it, so the check that it is present
    #: lives at the point of use (the `walk` command), not here.
    plex_url: str
    plex_token: str
    #: Mount roots to strip from a Plex playback path when deriving a
    #: path-based item_id for a title with no recognised external GUID
    #: (`identity.canonical_path`). Comma-separated in `PLEX_SOURCE_ROOTS`;
    #: empty by default, which means no stripping — always a safe default,
    #: never a wrong one.
    source_roots: tuple[str, ...]
    #: The Tautulli server `plexdb enrich-tautulli-plays` reads `get_history`
    #: from (issue #9, ADR-0004's optional-source pattern: only that command
    #: needs it, so the check that it's present lives at the point of use).
    #: Empty when unset.
    tautulli_url: str
    tautulli_api_key: str
    #: The TMDB v3 API key `plexdb enrich-tmdb-keywords` authenticates with
    #: (ADR-0004-style optional-source pattern: enrichment needs it, `init`
    #: and `walk` don't, so the check that it's present lives at the point of
    #: use). Empty when unset.
    tmdb_api_key: str
    #: Days a `tmdb_keywords` row stays fresh before it's eligible for
    #: re-fetch. Documented default sits mid-range of the 30-60 day window
    #: `docs/schema.md` sets for every external source.
    tmdb_keywords_stale_days: int
    #: Days a `tmdb_recommendations`/`tmdb_similar` edge set stays fresh
    #: before `plexdb enrich-tmdb-edges` re-fetches it. Same documented
    #: default and window as `tmdb_keywords_stale_days`, tracked separately.
    tmdb_edges_stale_days: int
    #: Path to `etv-station`'s `catalog.db`, opened read-only by `plexdb
    #: reconcile-etv` (issue #5) to compare `item_id` against `entry_id`.
    #: `None` when unset — like `snapshot_path`, this carries no default that
    #: points anywhere real, so a person is never silently pointed at
    #: whichever checkout happens to sit next to this one; the check that
    #: it's present lives at the point of use, the `reconcile-etv` command.
    etv_catalog_path: Path | None

    @classmethod
    def from_env(cls, *, env_file: Path | None = None) -> Config:
        """Build a config from `.env` plus the process environment.

        The process environment wins over the file, so a one-off override on the
        command line does not require editing `.env`.
        """
        load_dotenv(dotenv_path=env_file, override=False)
        raw = os.environ.get("PLEXDB_PATH", DEFAULT_STORE_PATH).strip()
        if not raw:
            raise ConfigError("PLEXDB_PATH is set but empty; unset it or give it a path")
        snapshot_raw = os.environ.get("PLEXDB_SNAPSHOT_PATH", "").strip()
        snapshot_path = Path(snapshot_raw).expanduser() if snapshot_raw else None
        source_roots_raw = os.environ.get("PLEX_SOURCE_ROOTS", "")
        source_roots = tuple(root.strip() for root in source_roots_raw.split(",") if root.strip())
        etv_catalog_raw = os.environ.get("ETV_CATALOG_PATH", "").strip()
        etv_catalog_path = Path(etv_catalog_raw).expanduser() if etv_catalog_raw else None
        return cls(
            store_path=Path(raw).expanduser(),
            snapshot_path=snapshot_path,
            plex_url=os.environ.get("PLEX_URL", "").strip(),
            plex_token=os.environ.get("PLEX_TOKEN", "").strip(),
            source_roots=source_roots,
            tautulli_url=os.environ.get("TAUTULLI_URL", "").strip(),
            tautulli_api_key=os.environ.get("TAUTULLI_API_KEY", "").strip(),
            tmdb_api_key=os.environ.get("TMDB_API_KEY", "").strip(),
            tmdb_keywords_stale_days=_stale_days_from_env(
                "TMDB_KEYWORDS_STALE_DAYS", _DEFAULT_TMDB_KEYWORDS_STALE_DAYS
            ),
            tmdb_edges_stale_days=_stale_days_from_env(
                "TMDB_EDGES_STALE_DAYS", _DEFAULT_TMDB_EDGES_STALE_DAYS
            ),
            etv_catalog_path=etv_catalog_path,
        )
