"""Reading a Plex library over its HTTP API — read-only, GET requests only.

The Frame harvester depends on `PlexSource`, not on `LivePlexClient` directly,
so a recorded-response test can substitute a fake that returns the same shapes
without a socket. The library walk, the history ingest and the media walk read
Plex through the Rust client in `crates/plexdb/src/plex.rs`. `LivePlexClient` is
the one implementation that talks to a real server; tests exercise it too, but
through `httpx`'s `MockTransport` playing back a recording rather than a live
connection.

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
#: Page size for `/library/sections/<key>/all`, small enough that each response
#: with `includeGuids=1` stays well inside `_DEFAULT_TIMEOUT`.
_SECTION_PAGE_SIZE = 500


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

    def part_file(self, rating_key: str, part_id: int) -> str | None:
        """The file Plex names for one part of one item, or `None` when the item has no
        such part. A metadata read, so it starts no playback session."""
        for record in self._get(f"/library/metadata/{rating_key}").get("Metadata") or []:
            for media in record.get("Media", []):
                for part in media.get("Part", []):
                    if int(part.get("id", -1)) == part_id and part.get("file"):
                        return str(part["file"])
        return None

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        # The token rides in a header, not the query string, so it never lands
        # in a proxy or access log.
        url = f"{self._base}{path}"
        try:
            resp = self._http.get(
                url,
                params=params,
                headers={"Accept": "application/json", "X-Plex-Token": self._token},
            )
            resp.raise_for_status()
        except httpx.HTTPError as err:
            raise PlexError(f"cannot reach Plex at {url}: {err}") from err
        try:
            body = resp.json()
        except ValueError:
            raise PlexError(f"Plex returned a response that is not JSON for {url}") from None
        if not isinstance(body, dict):
            raise PlexError(
                f"Plex returned a {type(body).__name__} body for {url}, expected an object"
            )
        container: dict[str, Any] = body.get("MediaContainer", {})
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
        """Every record of one Plex type in a section, fetched a page at a time so
        no single response runs into the client timeout. Stops at the container's
        `totalSize`, or on a short page when Plex reports none."""
        records: list[dict[str, Any]] = []
        start = 0
        while True:
            container = self._get(
                f"/library/sections/{section_key}/all",
                {
                    "type": type_,
                    "includeGuids": 1,
                    "X-Plex-Container-Start": start,
                    "X-Plex-Container-Size": _SECTION_PAGE_SIZE,
                },
            )
            page: list[dict[str, Any]] = container.get("Metadata", [])
            records.extend(page)
            start += len(page)
            total = container.get("totalSize")
            # `totalSize` is authoritative: a server that caps its page below the size
            # asked for must not truncate the list. Without it, a short page is the end.
            if not page or (
                start >= total if total is not None else len(page) < _SECTION_PAGE_SIZE
            ):
                break
        return records

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

    def accounts(self) -> list[dict[str, Any]]:
        """Every Plex account this server has ever attached — owner, home,
        and shared users alike — `/accounts` verbatim, including id 0.

        Confirmed live (2026-08-10): `MediaContainer.Account` entries carry
        `id` and `name`; id 0 is a placeholder account (empty `name` on this
        server) rather than a person. A caller excludes it by id, not by
        name — `enrich-tautulli-plays` needs this to resolve the server
        owner's id across systems (issue #26): Plex's history stores the
        owner under the local account id `1`, while Tautulli reports the
        same person under their plex.tv id, and this is the only endpoint
        that names both.
        """
        container = self._get("/accounts")
        accounts: list[dict[str, Any]] = container.get("Account", [])
        return accounts


class PlexAccountSource(Protocol):
    """The read surface `enrich-tautulli-plays` needs from Plex to resolve
    the server owner's account id across systems (issue #26) — real or
    recorded, independent of `PlexSource`'s library-walk surface."""

    def accounts(self) -> list[dict[str, Any]]:
        """Every Plex account this server has ever attached, including id
        0's placeholder record."""
        ...


def valid_accounts(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`accounts` (as returned by `accounts()` above), excluding Plex's id-0
    placeholder and any record with a blank name — the filter every caller
    needs before treating an entry as a real person. `latent_users.py` uses
    this to build its account-name lookup (issue #27)."""
    return [
        account
        for account in accounts
        if account.get("id") not in (None, 0) and account.get("name")
    ]
