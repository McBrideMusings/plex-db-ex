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


class TMDbError(PlexdbError):
    """TMDB could not be reached, or returned something unusable.

    A title TMDB has simply never heard of is not this — the client reports
    that as zero keywords, not an error, so a stale or wrong id doesn't stop
    a sweep over the rest of the library.
    """


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


class TautulliError(PlexdbError):
    """Tautulli could not be reached, or returned something unusable."""


class MDBListError(PlexdbError):
    """MDBList could not be reached, or returned something unusable.

    A list entry naming a title this store has never walked is not this — the
    harvest counts it as unresolved and drops it, so a list that mostly sits
    outside the library is an ordinary result rather than a failure.
    """


class EmbeddingError(PlexdbError):
    """The embedding server could not be reached, or returned something unusable."""


class JevError(PlexdbError):
    """Jev could not be reached, kept answering 429, or returned something unusable."""


class JevRejected(JevError):
    """Jev answered 400 or 422 for one pair: the request itself is refused, so asking
    again about the same pair gets the same answer."""


class MergeDecisionsError(PlexdbError):
    """`merge_decisions.json` is not valid JSON or does not hold the documented shape."""
