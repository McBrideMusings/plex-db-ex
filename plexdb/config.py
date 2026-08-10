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


@dataclass(frozen=True)
class Config:
    """Everything the store needs to know about its environment."""

    store_path: Path

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
        return cls(store_path=Path(raw).expanduser())
