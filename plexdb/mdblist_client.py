"""Reading MDBList's top lists and their entries over its HTTP API — read-only,
GET requests only.

`collections.py` depends on `MDBListSource`, not on `LiveMDBListClient`
directly, so a recorded-response test can substitute a fake returning the same
shapes without a socket — same split as `plex_client.py`/`PlexSource` and
`tmdb_client.py`/`TMDbSource`.

Verified against the live endpoints (2026-08-11):

`GET /lists/top` returns a JSON **array** of 50 lists, each shaped like

    {"id": 2194, "name": "Latest TV Shows", "slug": "latest-tv-shows",
     "user_name": "garycrawfordgc", "items": 300, "likes": 4509,
     "mediatype": "show", "dynamic": true}

`items` is the list's length and `likes` its follower count — both stored
verbatim, neither combined into anything.

`GET /lists/{id}/items` returns an **object, not an array**:

    {"movies": [...], "shows": [...],
     "pagination": {"limit": 1000, "offset": 0, "total": 300, "has_more": false}}

Three things about that shape are easy to get wrong, and each one has already
been got wrong in the client this module replaces
(`curator/curator/clients/mdblist.py`):

- Iterating the response walks the dict's *keys* — the strings `"movies"`,
  `"shows"`, `"pagination"` — not the entries. Both arrays are read here,
  separately, and the entry's own `mediatype` is carried through so a movie and
  a show are never resolved against each other's id space (schema v5).
- `has_more` means paging is mandatory. A list longer than the server's limit
  silently truncates otherwise, and the row count would then disagree with the
  `size` recorded next to it.
- An entry's own `rank` field is **not** its position in the list — the first
  entry of a 300-item list carries `"rank": 333`, so it is some other ordering
  entirely (a global one, as far as can be told from the response alone). This
  module ignores it and the caller uses array position, which is the ordering
  the list actually presents.

`POST /imdb/{movie|show}/` with `{"ids": [...]}` is the batch title endpoint
(verified 2026-10-05, recorded in `tests/fixtures/mdblist/batch_imdb_*.json`).
It answers a JSON array with one object per id it knows. An id it does not
know is simply absent, and the order is not the request's, so a caller
matches answers on `ids.imdb`. One POST costs one request against the daily
quota whatever its size; MDBList's documentation caps a batch at 200 ids. Each
title's `ratings` array holds one object per site, always the same sites:

    {"source": "imdb", "value": 7.9, "score": 79, "votes": 17303, "url": 5000}

`value` is the number on the site's own scale and is what this store keeps.
`score` is MDBList's 0–100 conversion of it, computed by MDBList, and is not
read (ADR-0012). Either can be null independently: rogerebert answers
`value: 4.0, score: null`, and a Metacritic entry can carry `votes: 1` with
`value: null`.

**A `User-Agent` header is required.** Without one, every endpoint answers
`403 Forbidden` with the body `error code: 1010` — including endpoints a valid
key is plainly entitled to. That reads as a dead credential and is not one, so
the agent is set explicitly here rather than left to whatever the HTTP library
defaults to.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .errors import MDBListError

_BASE = "https://api.mdblist.com"
_DEFAULT_TIMEOUT = 30.0

#: Sent on every request. See the module docstring: without an explicit agent
#: the service answers 403 to a perfectly good key.
_USER_AGENT = "plexdb"

#: The largest page the service is asked for. It caps this itself (observed:
#: 1000); asking for more is harmless and asking for less only costs requests.
_PAGE_LIMIT = 1000

#: Refuse to page forever. A `has_more` that never clears — a server bug, or a
#: list growing faster than it is read — would otherwise loop until something
#: else broke. At the observed limit this allows a list of 100,000 entries,
#: which is far past anything real.
_MAX_PAGES = 100

#: The most ids one batch title request carries — MDBList's documented cap.
BATCH_SIZE = 200


@dataclass(frozen=True)
class MDBListRating:
    """One site's rating of one title, as MDBList relayed it.

    `value` is the site's own number on the site's own scale, as the JSON
    carried it; `votes` its vote count. Either may be `None`.
    """

    site: str
    value: float | int | None
    votes: int | None


@dataclass(frozen=True)
class MDBListTitleRatings:
    """Every rating MDBList returned for one title, keyed back by IMDb id."""

    imdb_id: str
    ratings: tuple[MDBListRating, ...]


@dataclass(frozen=True)
class MDBListEntry:
    """One title on a list, as MDBList reported it.

    `external_ids` is every id the entry carried, in this store's own priority
    order, so the caller resolves through the strongest one available rather
    than insisting on TMDb. `media_type` is `"movie"` or `"show"` — the ids are
    scoped by it, because TMDB and TVDB number movies and shows in two separate
    lists that both start at 1 (schema v5).
    """

    external_ids: tuple[tuple[str, str], ...]
    media_type: str


@dataclass(frozen=True)
class MDBListList:
    """One list, as MDBList reported it. No derived values."""

    list_id: int
    name: str
    slug: str
    user_name: str
    #: The list's full length, whatever fraction of it this library holds.
    size: int | None
    #: MDBList's own follower count. Stored verbatim; never mixed with `size`.
    likes: int | None


class MDBListSource(Protocol):
    """The read surface a collection harvest needs from MDBList — real or recorded."""

    def top_lists(self) -> list[MDBListList]:
        """The lists MDBList currently ranks as top, in the order it returns them."""
        ...

    def list_entries(self, list_id: int) -> list[MDBListEntry]:
        """Every entry on one list, in the order the list presents them.

        Order is load-bearing: the caller turns array position into the stored
        `rank`. Paging is resolved here, so a caller never sees a partial list.
        """
        ...


class MDBListRatingsSource(Protocol):
    """The read surface the ratings sweep needs from MDBList — real or recorded."""

    def ratings(self, media_type: str, imdb_ids: Sequence[str]) -> list[MDBListTitleRatings]:
        """One batch request: the ratings of every id MDBList knows, in no
        particular order. `media_type` is `"movie"` or `"show"`; at most
        `BATCH_SIZE` ids. Raises `MDBListError` when the request fails."""
        ...


class LiveMDBListClient:
    """The one `MDBListSource` that reaches the real MDBList API, over `httpx`."""

    def __init__(self, api_key: str, http: httpx.Client | None = None) -> None:
        self._key = api_key
        self._http = http or httpx.Client(
            timeout=_DEFAULT_TIMEOUT, headers={"User-Agent": _USER_AGENT}
        )

    def top_lists(self) -> list[MDBListList]:
        body = self._get("/lists/top")
        if not isinstance(body, list):
            raise MDBListError(
                f"MDBList /lists/top returned {type(body).__name__}, expected a list of lists"
            )
        lists: list[MDBListList] = []
        for entry in body:
            list_id = entry.get("id")
            if list_id is None:
                continue
            lists.append(
                MDBListList(
                    list_id=int(list_id),
                    name=str(entry.get("name") or f"MDBList {list_id}"),
                    slug=str(entry.get("slug") or ""),
                    user_name=str(entry.get("user_name") or entry.get("user") or ""),
                    size=_optional_int(entry.get("items")),
                    likes=_optional_int(entry.get("likes")),
                )
            )
        return lists

    def list_entries(self, list_id: int) -> list[MDBListEntry]:
        entries: list[MDBListEntry] = []
        offset = 0
        for _ in range(_MAX_PAGES):
            body = self._get(
                f"/lists/{list_id}/items",
                extra={"limit": str(_PAGE_LIMIT), "offset": str(offset)},
            )
            if not isinstance(body, dict):
                raise MDBListError(
                    f"MDBList /lists/{list_id}/items returned {type(body).__name__}, "
                    "expected an object with movies/shows/pagination"
                )
            # Both arrays, separately — iterating `body` would walk the keys.
            # Movies before shows within a page keeps the order deterministic;
            # a list is overwhelmingly one or the other in practice.
            for media_type, key in (("movie", "movies"), ("show", "shows")):
                for item in body.get(key) or []:
                    ids = _external_ids(item)
                    if ids:
                        entries.append(MDBListEntry(external_ids=ids, media_type=media_type))
            pagination = body.get("pagination") or {}
            if not pagination.get("has_more"):
                return entries
            offset += _PAGE_LIMIT
        raise MDBListError(
            f"MDBList list {list_id} still reported more pages after {_MAX_PAGES} requests — "
            "refusing to page further"
        )

    def ratings(self, media_type: str, imdb_ids: Sequence[str]) -> list[MDBListTitleRatings]:
        if len(imdb_ids) > BATCH_SIZE:
            raise ValueError(f"a batch carries at most {BATCH_SIZE} ids, got {len(imdb_ids)}")
        path = f"/imdb/{media_type}/"
        started = time.monotonic()
        body = self._request("POST", path, body={"ids": list(imdb_ids)})
        if not isinstance(body, list):
            raise MDBListError(
                f"MDBList {path} returned {type(body).__name__}, expected a list of titles"
            )
        found = [title for title in map(_title_ratings, body) if title is not None]
        print(
            f"mdblist: POST {path} {len(imdb_ids)} id(s) -> {len(found)} title(s), "
            f"{sum(len(t.ratings) for t in found)} rating(s) in "
            f"{time.monotonic() - started:.1f} s",
            flush=True,
        )
        return found

    def _get(self, path: str, *, extra: dict[str, str] | None = None) -> Any:
        return self._request("GET", path, extra=extra)

    def _request(
        self,
        method: str,
        path: str,
        *,
        extra: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """Send one MDBList request, key in `params`, returning the parsed JSON body.

        The key rides in `params`, never in `url`, and every message below is
        built from `url` and `resp.status_code` rather than from the exception
        — httpx formats its own message as `"... for url '{response.url}'"`,
        query string included, so stringifying it would print the live key to
        stderr on any 401/429/5xx. `from None` for the same reason one level
        deeper: a chained cause keeps that key-bearing message reachable
        through `__cause__` and any traceback that walks it.
        """
        url = f"{_BASE}{path}"
        params = {"apikey": self._key}
        if extra:
            params.update(extra)
        try:
            resp = self._http.request(method, url, params=params, json=body)
        except httpx.HTTPError as err:
            raise MDBListError(f"cannot reach MDBList at {url}: {type(err).__name__}") from None
        if resp.status_code == 403:
            raise MDBListError(
                f"MDBList returned 403 for {url}. Either MDBLIST_API_KEY is not valid, or the "
                "request carried no User-Agent — the service answers 403 with body "
                "'error code: 1010' to an agentless request holding a perfectly good key."
            )
        try:
            resp.raise_for_status()
        except httpx.HTTPError:
            raise MDBListError(f"MDBList returned {resp.status_code} for {url}") from None
        try:
            return resp.json()
        except ValueError:
            raise MDBListError(f"MDBList returned a response that is not JSON for {url}") from None


def _optional_int(value: Any) -> int | None:
    """A whole number the source supplied, or `None` where it supplied nothing.

    Deliberately not `int(value or 0)`: zero is a real answer — a brand-new
    list genuinely has no likes — and collapsing "absent" onto it would make
    "MDBList publishes no number for this" indistinguishable from "MDBList says
    zero".
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _title_ratings(title: Any) -> MDBListTitleRatings | None:
    """One batch answer's IMDb id and ratings, or `None` for an answer with no
    IMDb id to match it back by."""
    if not isinstance(title, dict):
        return None
    imdb_id = (title.get("ids") or {}).get("imdb")
    if not imdb_id:
        return None
    ratings: list[MDBListRating] = []
    for rating in title.get("ratings") or []:
        if not isinstance(rating, dict) or not rating.get("source"):
            continue
        value = rating.get("value")
        ratings.append(
            MDBListRating(
                site=str(rating["source"]),
                value=value if isinstance(value, int | float) else None,
                votes=_optional_int(rating.get("votes")),
            )
        )
    return MDBListTitleRatings(imdb_id=str(imdb_id), ratings=tuple(ratings))


def _external_ids(item: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Every id one entry carries, in this store's own priority order.

    MDBList nests them under `ids` (`{"imdb": ..., "tmdb": ..., "tvdb": ...}`)
    and also repeats some at the top level (`imdb_id`, `tvdb_id`, `id`). Both
    are read, `ids` first, so an entry that only carries the flat form still
    resolves. `plex` is absent from MDBList entirely and simply never appears.
    """
    nested = item.get("ids") or {}
    candidates: list[tuple[str, Any]] = [
        ("imdb", nested.get("imdb") or item.get("imdb_id")),
        ("tmdb", nested.get("tmdb") or item.get("id")),
        ("tvdb", nested.get("tvdb") or item.get("tvdb_id")),
    ]
    return tuple((ns, str(value)) for ns, value in candidates if value not in (None, ""))
