"""Reading TMDB's keywords endpoint over its HTTP API — read-only, GET requests only.

`enrich_tmdb.py` depends on `TMDbSource`, not on `LiveTMDbClient` directly, so a
recorded-response test can substitute a fake that returns the same shapes
without a socket — same split as `plex_client.py`/`PlexSource`.

Verified against the live endpoints (2026-08-10): a movie's keywords live at
`/movie/{id}/keywords` as `{"id": ..., "keywords": [{"id", "name"}, ...]}`; a
show's live at `/tv/{id}/keywords` as `{"id": ..., "results": [{"id", "name"}, ...]}`
— same `keywords` vs `results` split TMDB uses on the `append_to_response`
sub-object. An id TMDB has never heard of returns 404 with a JSON error body,
handled here as zero keywords rather than an error: a stale or wrong id in
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


class LiveTMDbClient:
    """The one `TMDbSource` that reaches the real TMDB API, over `httpx`."""

    def __init__(self, api_key: str, http: httpx.Client | None = None) -> None:
        self._key = api_key
        self._http = http or httpx.Client(timeout=_DEFAULT_TIMEOUT)

    def keywords(self, tmdb_id: str, media_type: str) -> list[str]:
        path = "movie" if media_type == "movie" else "tv"
        # `url` never carries the key — it rides in `params` instead — so every
        # error message below is built from `url`/`resp.status_code`, never
        # from `err` or `resp.url` directly: httpx's own `raise_for_status`
        # formats its message as `"... for url '{response.url}'"`, and that
        # URL includes the query string, so stringifying the exception itself
        # would print the live API key to stderr on any 401/429/5xx.
        url = f"{_BASE}/{path}/{tmdb_id}/keywords"
        try:
            resp = self._http.get(url, params={"api_key": self._key})
        except httpx.HTTPError as err:
            raise TMDbError(f"cannot reach TMDB at {url}: {type(err).__name__}") from err
        if resp.status_code == 404:
            return []
        try:
            resp.raise_for_status()
        except httpx.HTTPError as err:
            raise TMDbError(f"TMDB returned {resp.status_code} for {url}") from err
        body: dict[str, Any] = resp.json()
        raw = body.get("keywords") if path == "movie" else body.get("results")
        return [str(entry["name"]) for entry in (raw or []) if entry.get("name")]
