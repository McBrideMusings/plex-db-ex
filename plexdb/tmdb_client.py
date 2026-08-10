"""Reading TMDB's keywords, recommendations, and similar endpoints over its HTTP
API — read-only, GET requests only.

`enrich_tmdb.py` and `tmdb_edges.py` depend on `TMDbSource`, not on
`LiveTMDbClient` directly, so a recorded-response test can substitute a fake
that returns the same shapes without a socket — same split as
`plex_client.py`/`PlexSource`.

Verified against the live endpoints (2026-08-10): a movie's keywords live at
`/movie/{id}/keywords` as `{"id": ..., "keywords": [{"id", "name"}, ...]}`; a
show's live at `/tv/{id}/keywords` as `{"id": ..., "results": [{"id", "name"}, ...]}`
— same `keywords` vs `results` split TMDB uses on the `append_to_response`
sub-object. `recommendations` and `similar` both live at
`/{movie,tv}/{id}/{recommendations,similar}` as
`{"page", "results": [{"id", ...}, ...], "total_pages", "total_results"}`,
ordered best-first with no explicit rank field of its own — array position
*is* the rank. Only one page is ever fetched: TMDB returns 20 results per
page and every recorded response seen carries a handful of pages at most, so
the first page already carries the signal this store cares about. Only
`recommendations` results carry a `media_type` field in the body; `similar`
does not — both are read by path segment instead, since a type-specific
endpoint only ever returns same-type results.
An id TMDB has never heard of returns 404 with a JSON error body, handled
here as an empty result rather than an error: a stale or wrong id in
`external_ids` must not stop a sweep over the rest of the library.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from .errors import TMDbError

_BASE = "https://api.themoviedb.org/3"
_DEFAULT_TIMEOUT = 30.0


class TMDbSource(Protocol):
    """The read surface an enrichment sweep needs from TMDB — real or recorded."""

    def keywords(self, tmdb_id: str, media_type: str) -> list[str]:
        """Keyword names TMDB reports for one title.

        `media_type` is `"movie"` or `"tv"` — the two shapes this store
        enriches (`items.type` `"movie"` and `"show"` respectively). Returns
        an empty list both when TMDB has the title but no keywords for it,
        and when TMDB has never heard of the id — the two are indistinguishable
        to a caller that only wants the tags, and both are cached the same
        way by the sweep that calls this.
        """
        ...

    def recommendations(self, tmdb_id: str, media_type: str) -> list[str]:
        """TMDB ids TMDB recommends for one title, best match first.

        Behavioural signal: "people who engaged with this also engaged with
        that". Empty both when TMDB has nothing to recommend and when TMDB
        has never heard of `tmdb_id` — same non-error treatment as
        `keywords`.
        """
        ...

    def similar(self, tmdb_id: str, media_type: str) -> list[str]:
        """TMDB ids TMDB calls similar to one title, best match first.

        Content-derived signal, not behavioural — kept as a distinct edge
        type from `recommendations` because the two measure different
        things. Empty in the same two cases as `recommendations`.
        """
        ...


class LiveTMDbClient:
    """The one `TMDbSource` that reaches the real TMDB API, over `httpx`."""

    def __init__(self, api_key: str, http: httpx.Client | None = None) -> None:
        self._key = api_key
        self._http = http or httpx.Client(timeout=_DEFAULT_TIMEOUT)

    def keywords(self, tmdb_id: str, media_type: str) -> list[str]:
        path = "movie" if media_type == "movie" else "tv"
        body = self._get(f"{path}/{tmdb_id}/keywords")
        if body is None:
            return []
        raw = body.get("keywords") if path == "movie" else body.get("results")
        return [str(entry["name"]) for entry in (raw or []) if entry.get("name")]

    def recommendations(self, tmdb_id: str, media_type: str) -> list[str]:
        return self._related(tmdb_id, media_type, "recommendations")

    def similar(self, tmdb_id: str, media_type: str) -> list[str]:
        return self._related(tmdb_id, media_type, "similar")

    def _related(self, tmdb_id: str, media_type: str, endpoint: str) -> list[str]:
        path = "movie" if media_type == "movie" else "tv"
        body = self._get(f"{path}/{tmdb_id}/{endpoint}")
        if body is None:
            return []
        results = body.get("results") or []
        # Array position is the rank TMDB gave — `enumerate` in the caller
        # turns this order into the stored `rank`, so this list's order is
        # load-bearing, not incidental.
        return [str(entry["id"]) for entry in results if entry.get("id") is not None]

    def _get(self, path: str) -> dict[str, Any] | None:
        """GET one TMDB path, `params`-only key, returning the parsed JSON body
        or `None` for a 404 — the one response shape every endpoint here
        treats as "nothing", not an error.

        `url` never carries the key — it rides in `params` instead — so every
        error message below is built from `url`/`resp.status_code`, never
        from `err` or `resp.url` directly: httpx's own `raise_for_status`
        formats its message as `"... for url '{response.url}'"`, and that
        URL includes the query string, so stringifying the exception itself
        would print the live API key to stderr on any 401/429/5xx.

        `from None`, not `from err`, for the same reason one level deeper: a
        chained cause travels with the exception, so `raise ... from err`
        leaves httpx's key-bearing message reachable through `__cause__` and
        printed by any traceback — an unhandled crash, a logger with
        `exc_info`, a CI failure dump. Severing the chain is what actually
        keeps the key out; the status code and URL below carry everything the
        message needed from it anyway.
        """
        url = f"{_BASE}/{path}"
        try:
            resp = self._http.get(url, params={"api_key": self._key})
        except httpx.HTTPError as err:
            raise TMDbError(f"cannot reach TMDB at {url}: {type(err).__name__}") from None
        if resp.status_code == 404:
            return None
        try:
            resp.raise_for_status()
        except httpx.HTTPError:
            raise TMDbError(f"TMDB returned {resp.status_code} for {url}") from None
        result: dict[str, Any] = resp.json()
        return result
