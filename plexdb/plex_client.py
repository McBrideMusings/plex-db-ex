"""Reading a Plex library over its HTTP API — read-only, GET requests only.

The walk (`plexdb/walk.py`) depends on `PlexSource`, not on `LivePlexClient`
directly, so a recorded-response test can substitute a fake that returns the
same shapes without a socket. `LivePlexClient` is the one implementation that
talks to a real server; tests exercise it too, but through `httpx`'s
`MockTransport` playing back a recording rather than a live connection.

Section types and metadata types follow Plex's own numbering
(https://support.plex.tv): a library section is `movie` or `show` in its
`Directory` listing, while an item fetched from `/library/sections/<key>/all`
is typed `1` (movie), `2` (show), or `4` (episode). A show section is walked
twice — once at type 2 for the show-level record (its own external ids), once
at type 4 for its episodes — because an episode's own `Guid` list is the
episode's identity, not the show's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .errors import PlexError

#: Plex's own numbering for `/library/sections/<key>/all?type=<n>`.
PLEX_TYPE_MOVIE = 1
PLEX_TYPE_SHOW = 2
PLEX_TYPE_EPISODE = 4

_DEFAULT_TIMEOUT = 60.0


@dataclass(frozen=True)
class Section:
    """One library section, as listed by `/library/sections`."""

    key: str
    #: Plex's section type: "movie", "show", or something the walk skips (e.g. "photo").
    type: str
    title: str


class PlexSource(Protocol):
    """The read surface a walk needs from Plex — real or recorded.

    Two methods, both read-only: the section list, and the raw `Metadata`
    records for one section at one Plex item type. Everything the walk
    knows about a title comes out of those records; `PlexSource` carries no
    parsing, so a recorded fixture and a live response are interchangeable
    here.
    """

    def sections(self) -> list[Section]:
        """Every library section the server reports, in Plex's own order."""
        ...

    def items(self, section_key: str, type_: int) -> list[dict[str, Any]]:
        """Raw `Metadata` records for one section at one Plex item type.

        `type_` is one of the `PLEX_TYPE_*` constants above. A section with no
        matching records (e.g. asking a movie section for `PLEX_TYPE_SHOW`
        items) returns an empty list, never an error.
        """
        ...


class LivePlexClient:
    """The one `PlexSource` that reaches a real server, over `httpx`.

    Read-only by construction: every method issues a GET, and nothing here
    can mutate the library. `http` is injectable so a test can hand in a
    client built on `httpx.MockTransport` and exercise this exact
    request-building and JSON-parsing code against a recorded response,
    without a socket.
    """

    def __init__(self, base_url: str, token: str, http: httpx.Client | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._http = http or httpx.Client(timeout=_DEFAULT_TIMEOUT)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        # The token rides in a header, not the query string, so it never lands
        # in a proxy or access log.
        try:
            resp = self._http.get(
                f"{self._base}{path}",
                params=params,
                headers={"Accept": "application/json", "X-Plex-Token": self._token},
            )
            resp.raise_for_status()
        except httpx.HTTPError as err:
            raise PlexError(f"cannot reach Plex at {self._base}{path}: {err}") from err
        container: dict[str, Any] = resp.json().get("MediaContainer", {})
        return container

    def sections(self) -> list[Section]:
        container = self._get("/library/sections")
        sections = []
        for entry in container.get("Directory", []):
            key = entry.get("key")
            kind = entry.get("type")
            if key is None or kind is None:
                continue
            sections.append(Section(key=str(key), type=str(kind), title=entry.get("title") or ""))
        return sections

    def items(self, section_key: str, type_: int) -> list[dict[str, Any]]:
        container = self._get(
            f"/library/sections/{section_key}/all",
            {"type": type_, "includeGuids": 1},
        )
        metadata: list[dict[str, Any]] = container.get("Metadata", [])
        return metadata
