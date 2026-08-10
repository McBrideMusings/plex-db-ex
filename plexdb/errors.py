"""The errors a user is meant to see.

Anything raised as one of these reaches the command line as a single `error: …`
line. Anything else reaches them as a traceback, which is a bug — a person who
pointed `PLEXDB_PATH` at the wrong thing should be told what is wrong with it,
not handed a stack.
"""

from __future__ import annotations


class PlexdbError(RuntimeError):
    """Base for every failure this package reports to a person."""


class ConfigError(PlexdbError):
    """A required setting is missing or unusable."""


class StoreError(PlexdbError):
    """The store could not be opened, created, or trusted."""


class PlexError(PlexdbError):
    """The Plex server could not be reached, or returned something unusable."""
