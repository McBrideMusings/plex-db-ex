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


class TautulliError(PlexdbError):
    """Tautulli could not be reached, or returned something unusable."""


class EncoderError(PlexdbError):
    """The Frame encoder refused a request or returned something unusable (ADR-0025)."""


class EncoderUnavailable(EncoderError):
    """The Frame encoder did not answer: the connection was refused or timed out, or it
    answered 5xx. Every later request would fail the same way, so the harvester stops
    until its next window rather than recording a failure against each title."""


class MediaError(PlexdbError):
    """A title's video could not be read through Plex or decoded: the part is unreadable,
    ffmpeg or ffprobe exited non-zero, or neither is installed. It fails that title only.

    `str(err)` is the full message and may name paths and carry ffmpeg's stderr. `reason`
    is a short fixed phrase with neither, safe to show on a page with no login."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class MediaTimeout(MediaError):
    """ffmpeg or ffprobe ran past its time limit on a file. Unlike a missing binary, this
    says something about the file, so a caller may record it."""


class DecisionsFileError(PlexdbError):
    """A Plex DB-EX decisions file cannot be read or written, is not valid JSON, or does
    not hold the documented shape."""


class MergeDecisionsError(DecisionsFileError):
    """`merge_decisions.json` is not valid JSON or does not hold the documented shape."""


class RoleDecisionsError(DecisionsFileError):
    """`role_decisions.json` is not valid JSON or does not hold the documented shape."""


class NameDecisionsError(DecisionsFileError):
    """`name_decisions.json` is not valid JSON or does not hold the documented shape."""


class ListDecisionsError(DecisionsFileError):
    """`list_decisions.json` is not valid JSON or does not hold the documented shape."""


class NoSuchPanel(LookupError):
    """A panel write named a dashboard or panel `explore-panels.json` does not hold."""


class PanelsFileError(PlexdbError):
    """`explore-panels.json` or `panel-history.json` cannot be read or written, is not valid
    JSON, or does not hold the documented shape."""
