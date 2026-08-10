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
#: Page size for `/status/sessions/history/all`, which does not return
#: everything in one response the way `/library/sections/<key>/all` does — a
#: real server has tens of thousands of history rows.
_HISTORY_PAGE_SIZE = 200


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

    def collections(self, section_key: str) -> list[dict[str, Any]]:
        """Every collection Plex lists for one section, smart and regular alike.

        The `smart` flag Plex returns — an auto-generated saved search versus
        a set someone actually built — is a caller's decision, not filtered
        here. Confirmed live against a real server: `smart` comes back as the
        string `"1"` on a saved search and is absent (`None`) on a regular
        collection, never a JSON boolean.
        """
        container = self._get(f"/library/sections/{section_key}/collections")
        metadata: list[dict[str, Any]] = container.get("Metadata", [])
        return metadata

    def collection_children(self, collection_key: str) -> list[dict[str, Any]]:
        """The member records of one collection, addressed by its own `ratingKey`.

        This is the same `/library/metadata/<ratingKey>/children` endpoint
        Plex uses for a show's seasons, since a collection is a container like
        any other. Confirmed live: each member record carries its own
        `ratingKey`, resolvable through `plex_items`.
        """
        container = self._get(f"/library/metadata/{collection_key}/children")
        metadata: list[dict[str, Any]] = container.get("Metadata", [])
        return metadata

    def history(self, *, since_viewed_at: int | None = None) -> list[dict[str, Any]]:
        """Every history event at or newer than `since_viewed_at`, newest
        first.

        Filters server-side via Plex's own `viewedAt>` operator — confirmed
        live: a filtered request reports a smaller `totalSize` than an
        unfiltered one, so this is a real narrowing, not just a client-side
        illusion. `None` fetches the whole history, which is what a first
        ingest needs.

        `viewedAt>` is **inclusive** in practice despite its name — filtering
        on a value equal to the newest event's own `viewedAt` still returns
        that event (confirmed live). So a caller passing the latest
        `viewed_at` it already has on file will see that same event again on
        the next call; `ingest_plays`'s `ON CONFLICT(history_key) DO NOTHING`
        is what makes that free rather than a duplicate.

        Paginates via `X-Plex-Container-Start`/`-Size`, since unlike a
        library section this can be tens of thousands of rows.
        """
        params: dict[str, Any] = {"sort": "viewedAt:desc"}
        if since_viewed_at is not None:
            params["viewedAt>"] = since_viewed_at

        events: list[dict[str, Any]] = []
        start = 0
        while True:
            container = self._get(
                "/status/sessions/history/all",
                {
                    **params,
                    "X-Plex-Container-Start": start,
                    "X-Plex-Container-Size": _HISTORY_PAGE_SIZE,
                },
            )
            page: list[dict[str, Any]] = container.get("Metadata", [])
            events.extend(page)
            start += len(page)
            total = container.get("totalSize", start)
            if not page or start >= total:
                break
        return events

    def devices(self) -> list[dict[str, Any]]:
        """Every device Plex has ever seen a client connect from.

        Cheap to call once per ingest and cache — a play references a
        device far more often than the device list changes.
        """
        container = self._get("/devices")
        devices: list[dict[str, Any]] = container.get("Device", [])
        return devices
