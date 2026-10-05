"""Reading anime tags from AniList's GraphQL API, and the Fribb mapping that
finds a title's AniList ids — read-only, keyless.

`enrich_anilist.py` depends on `AniListSource`, not on `LiveAniListClient`
directly, so a recorded-response test substitutes a fake without a socket —
the same split as `wikidata_client.py`/`WikidataSource`.

AniList carries no TMDB, IMDb or TVDB id; its only cross id is `idMal`. Fribb's
`anime-list-full.json` (https://github.com/Fribb/anime-lists) is the bridge: one
entry per AniList/MAL title with the TMDB, IMDb and TVDB ids it corresponds to.
It is fetched whole, once per sweep — 7.5 MB, 0.4 s from GitHub on 2026-10-03.

Tags come from `Page { media(id_in: [...]) }`, up to `PAGE_SIZE` ids per
request. An id AniList has no entry for is simply absent from the page. The
API's limit was 30 requests a minute on 2026-10-03 (`X-RateLimit-Limit: 30`),
so the client pauses `_PAUSE_SECONDS` after every request and stays under it
with one request in flight. A 429 carries `Retry-After`, which it honours a
bounded number of times (https://docs.anilist.co/guide/rate-limiting).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from typing import Any, NamedTuple, Protocol

import httpx

from .errors import AniListError

ENDPOINT = "https://graphql.anilist.co"
MAPPING_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"

#: AniList's own ceiling on `perPage`. A request asks for at most this many ids,
#: so every match fits on the first page and no request ever asks for a second.
PAGE_SIZE = 50

QUERY = """query ($ids: [Int], $perPage: Int) {
  Page(perPage: $perPage) {
    media(id_in: $ids, type: ANIME) {
      id
      tags { name rank category isMediaSpoiler isGeneralSpoiler }
    }
  }
}"""

_TIMEOUT = 30.0
#: 60 s / 30 requests, plus a margin, so one request in flight never trips the limit.
_PAUSE_SECONDS = 2.1
#: 429s honoured for one request before it counts as failed.
_MAX_RETRIES = 3
#: A `Retry-After` longer than this fails the batch rather than stalling a sweep.
_MAX_RETRY_AFTER = 120.0


class Tag(NamedTuple):
    """One AniList tag on one media entry, as AniList gave it."""

    name: str
    #: AniList's 0–100 rank: the share of voters who agree the tag applies.
    rank: int | None
    category: str | None
    #: `isMediaSpoiler or isGeneralSpoiler`.
    spoiler: bool


class AniListSource(Protocol):
    """The read surface the AniList sweep needs — real or recorded."""

    def mapping(self) -> list[dict[str, Any]]:
        """Fribb's `anime-list-full.json`, as its list of entries."""
        ...

    def tags(self, anilist_ids: Sequence[int]) -> dict[int, list[Tag]]:
        """Every tag on each of these AniList ids. An id AniList has no entry
        for is absent from the result."""
        ...


def parse_media(body: dict[str, Any]) -> dict[int, list[Tag]]:
    """One page of GraphQL JSON → tags per AniList id. A tag with no name is dropped."""
    page = (body.get("data") or {}).get("Page") or {}
    out: dict[int, list[Tag]] = {}
    for media in page.get("media") or []:
        if not isinstance(media, dict) or not isinstance(media.get("id"), int):
            continue
        tags: list[Tag] = []
        for raw in media.get("tags") or []:
            name = raw.get("name") if isinstance(raw, dict) else None
            if not isinstance(name, str) or not name.strip():
                continue
            rank = raw.get("rank")
            tags.append(
                Tag(
                    name=name,
                    rank=rank if isinstance(rank, int) else None,
                    category=raw.get("category") if isinstance(raw.get("category"), str) else None,
                    spoiler=bool(raw.get("isMediaSpoiler")) or bool(raw.get("isGeneralSpoiler")),
                )
            )
        out[media["id"]] = tags
    return out


class LiveAniListClient:
    """The one `AniListSource` that reaches AniList and GitHub, over `httpx`."""

    def __init__(
        self,
        http: httpx.Client | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http or httpx.Client(timeout=_TIMEOUT)
        self._sleep = sleep

    def mapping(self) -> list[dict[str, Any]]:
        started = time.monotonic()
        try:
            resp = self._http.get(MAPPING_URL, follow_redirects=True)
        except httpx.HTTPError as err:
            raise AniListError(
                f"cannot reach the anime mapping at {MAPPING_URL}: {type(err).__name__}"
            ) from None
        if resp.status_code != 200:
            raise AniListError(f"the anime mapping returned {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            raise AniListError("the anime mapping is not JSON") from None
        if not isinstance(body, list):
            raise AniListError("the anime mapping is not a list of entries")
        entries = [e for e in body if isinstance(e, dict)]
        print(
            f"anilist: mapping {len(entries)} entries in {time.monotonic() - started:.1f} s",
            flush=True,
        )
        return entries

    def tags(self, anilist_ids: Sequence[int]) -> dict[int, list[Tag]]:
        out: dict[int, list[Tag]] = {}
        ids = list(dict.fromkeys(anilist_ids))
        for start in range(0, len(ids), PAGE_SIZE):
            out.update(self._request(ids[start : start + PAGE_SIZE]))
        return out

    def _request(self, ids: list[int]) -> dict[int, list[Tag]]:
        for attempt in range(_MAX_RETRIES + 1):
            started = time.monotonic()
            try:
                resp = self._http.post(
                    ENDPOINT,
                    json={"query": QUERY, "variables": {"ids": ids, "perPage": PAGE_SIZE}},
                    headers={"Accept": "application/json"},
                )
            except httpx.HTTPError as err:
                raise AniListError(
                    f"cannot reach AniList at {ENDPOINT}: {type(err).__name__}"
                ) from None
            elapsed = time.monotonic() - started
            if resp.status_code == 429:
                if attempt == _MAX_RETRIES:
                    break
                wait = _retry_after(resp)
                print(f"anilist: 429 after {elapsed:.1f} s, waiting {wait:.0f} s", flush=True)
                self._sleep(wait)
                continue
            self._sleep(_PAUSE_SECONDS)
            if resp.status_code != 200:
                raise AniListError(
                    f"AniList returned {resp.status_code} for {len(ids)} id(s) "
                    f"after {elapsed:.1f} s"
                )
            try:
                body: dict[str, Any] = resp.json()
            except ValueError:
                raise AniListError("AniList returned a response that is not JSON") from None
            if body.get("errors"):
                messages = "; ".join(
                    str(e.get("message")) for e in body["errors"] if isinstance(e, dict)
                )
                raise AniListError(f"AniList refused the query: {messages}")
            found = parse_media(body)
            print(
                f"anilist: {len(ids)} id(s) -> {len(found)} media, "
                f"{sum(len(t) for t in found.values())} tag(s) in {elapsed:.1f} s",
                flush=True,
            )
            return found
        raise AniListError(f"AniList kept answering 429 after {_MAX_RETRIES} retries")


def _retry_after(resp: httpx.Response) -> float:
    raw = resp.headers.get("Retry-After", "").strip()
    try:
        wait = float(raw)
    except ValueError:
        wait = 60.0
    if math.isnan(wait):
        wait = 60.0
    if wait > _MAX_RETRY_AFTER:
        raise AniListError(f"AniList asked for a {wait:.0f} s wait; giving up on this batch")
    return max(wait, 1.0)
