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


class WikidataError(PlexdbError):
    """Wikidata's query service could not be reached, refused the query, or returned
    something unusable.

    An IMDb id Wikidata has no item for is not this — the query simply returns no
    rows for it, and the sweep caches that as an empty result.
    """


class AniListError(PlexdbError):
    """AniList's GraphQL API or the Fribb anime mapping could not be reached,
    refused the request, or returned something unusable.

    An AniList id AniList has no entry for is not this — the page simply omits it,
    and the sweep caches that title as an empty result.
    """


class LetterboxdError(PlexdbError):
    """Letterboxd could not be reached, refused the request (a Cloudflare challenge
    answers 403), or sent the client somewhere robots.txt disallows.

    A TMDB id Letterboxd has no film for is not this — the sweep caches it as not
    listed — and neither is a film page without the film marker, which the sweep
    counts as a parse failure.
    """


class TautulliError(PlexdbError):
    """Tautulli could not be reached, or returned something unusable."""


class MDBListError(PlexdbError):
    """MDBList could not be reached, or returned something unusable.

    A list entry naming a title this store has never walked is not this — the
    harvest counts it as unresolved and drops it, so a list that mostly sits
    outside the library is an ordinary result rather than a failure.

    `status` is the HTTP status the service answered, or `None` where it never
    answered.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class MDBListQuotaError(MDBListError):
    """MDBList answered 429: the account's request quota is spent. Every later
    request would get the same answer, so a run stops rather than retrying."""


class MDBListUnreachable(MDBListError):
    """MDBList never answered. Every list would fail the same way, so a harvest stops
    rather than recording the failure against each list."""


class EmbeddingError(PlexdbError):
    """The embedding server could not be reached, or returned something unusable."""


class EncoderError(PlexdbError):
    """The Frame encoder refused a request or returned something unusable (ADR-0025)."""


class EncoderUnavailable(EncoderError):
    """The Frame encoder did not answer: the connection was refused or timed out, or it
    answered 5xx. Every later request would fail the same way, so the harvester stops
    until its next window rather than recording a failure against each title."""


class HarvesterBusy(PlexdbError):
    """Another frame harvester holds the one slot; the message names its pid and title."""


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


class JevError(PlexdbError):
    """Jev could not be reached, kept answering 429, or returned something unusable."""


class JevRejected(JevError):
    """Jev answered 400 or 422 for one pair or keyword: the request itself is refused,
    so asking again about the same one gets the same answer."""


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
