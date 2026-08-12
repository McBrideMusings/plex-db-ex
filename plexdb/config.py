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

__all__ = ["DEFAULT_SHARED_ACCOUNT_IDS", "DEFAULT_STORE_PATH", "Config", "ConfigError"]

#: Where the store lands when nothing says otherwise. A path is not a secret,
#: so unlike a URL or a token this one may carry a default.
DEFAULT_STORE_PATH = "./data/plexdb.db"

#: The only two Plex accounts this server's owner has confirmed are actually
#: shared by more than one person (issue #27): account `1` (the server
#: owner, McBrideMusings) and `3670670` (bboy2448). Every other account —
#: however many devices or IPs it shows — is one named person; which
#: accounts are shared is configuration the owner stated, never something
#: `plexdb latent-users` infers from device counts. An account id is not a
#: secret, so unlike a URL or a token this one may carry a real default.
DEFAULT_SHARED_ACCOUNT_IDS = "1,3670670"

# Staleness windows and source credentials are deliberately absent from here.
# A Gated Source reads its own `<NAME>_STALE_DAYS` and its own credential
# variable (`plexdb/sources.py`), against the single `DEFAULT_STALE_DAYS` in
# `staleness.py`. That keeps each source's window independently dialable — the
# reason three separate knobs existed — while adding a source touches no file
# but its own, and it lets this module stay a leaf importing no feature module
# (issue #20), which is what the three duplicated literals here were buying.


def _shared_account_ids_from_env() -> tuple[int, ...]:
    """`PLEXDB_SHARED_ACCOUNT_IDS`, comma-separated, falling back to
    `DEFAULT_SHARED_ACCOUNT_IDS` when unset. An explicitly blank value means
    zero shared accounts, not "use the default" — the opposite of the
    convention `GatedSource.resolve_stale_days` uses, where a blank value means
    the default, because there a zero would re-fetch the whole library."""
    raw = os.environ.get("PLEXDB_SHARED_ACCOUNT_IDS", DEFAULT_SHARED_ACCOUNT_IDS)
    if not raw.strip():
        return ()
    try:
        return tuple(int(part.strip()) for part in raw.split(",") if part.strip())
    except ValueError as err:
        raise ConfigError(
            "PLEXDB_SHARED_ACCOUNT_IDS must be a comma-separated list of whole numbers, "
            f"got {raw!r}"
        ) from err


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
    #: (`identity.canonical_path`). Comma-separated in `PLEX_SOURCE_ROOTS`.
    #:
    #: **Empty is not a safe default — it is the wrong answer** (issue #24).
    #: With no root to strip, an item_id for a GUID-less title is a hash of
    #: wherever the disk happens to be mounted, so two correct installs
    #: against one Plex server name the same file differently. Measured: 1,521
    #: titles disagreed with `etv-station` for exactly this reason. `walk`
    #: therefore refuses to run while this is empty rather than writing ids
    #: that look fine and join with nothing.
    source_roots: tuple[str, ...]
    #: Plex account ids `plexdb latent-users` treats as genuinely shared by
    #: more than one person, and therefore clusters by device fingerprint
    #: (issue #27). Every account not in this tuple is reported as one named
    #: person, however many devices or IPs it shows — device fingerprinting
    #: exists only to split apart the accounts named here. Comma-separated
    #: in `PLEXDB_SHARED_ACCOUNT_IDS`; defaults to `DEFAULT_SHARED_ACCOUNT_IDS`.
    shared_account_ids: tuple[int, ...]
    #: The Tautulli server `plexdb enrich-tautulli-plays` reads `get_history`
    #: from (issue #9, ADR-0004's optional-source pattern: only that command
    #: needs it, so the check that it's present lives at the point of use).
    #: Empty when unset.
    tautulli_url: str
    tautulli_api_key: str
    #: Path to `etv-station`'s `catalog.db`, opened read-only by `plexdb
    #: reconcile-etv` (issue #5) to compare `item_id` against `entry_id`.
    #: `None` when unset — like `snapshot_path`, this carries no default that
    #: points anywhere real, so a person is never silently pointed at
    #: whichever checkout happens to sit next to this one; the check that
    #: it's present lives at the point of use, the `reconcile-etv` command.
    etv_catalog_path: Path | None
    #: Where `plexdb init` puts the copy it takes before applying a migration.
    #: Defaults to a `backups/` directory beside the store, so the container
    #: needs no extra mount and a dev checkout needs no extra setting — the
    #: copy lands next to the thing it is a copy of. Every copy is kept:
    #: migrations are forward-only, so a copy is the only route back past its
    #: own migration.
    backup_dir: Path

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
        store_path = Path(raw).expanduser()
        backup_raw = os.environ.get("PLEXDB_BACKUP_DIR", "").strip()
        backup_dir = (
            Path(backup_raw).expanduser() if backup_raw else store_path.parent / "backups"
        )
        return cls(
            store_path=store_path,
            snapshot_path=snapshot_path,
            plex_url=os.environ.get("PLEX_URL", "").strip(),
            plex_token=os.environ.get("PLEX_TOKEN", "").strip(),
            source_roots=source_roots,
            shared_account_ids=_shared_account_ids_from_env(),
            tautulli_url=os.environ.get("TAUTULLI_URL", "").strip(),
            tautulli_api_key=os.environ.get("TAUTULLI_API_KEY", "").strip(),
            etv_catalog_path=etv_catalog_path,
            backup_dir=backup_dir,
        )
