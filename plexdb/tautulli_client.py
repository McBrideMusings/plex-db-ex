"""Reading Tautulli's watch history over its HTTP API — read-only, GET requests only.

`plays.match_tautulli_history` depends on `TautulliSource`, not on
`LiveTautulliClient` directly, so a recorded-response test can substitute a
fake that returns the same shapes without a socket — the same split
`plex_client.py`/`HistorySource` and `tmdb_client.py`/`TMDbSource` use.

Verified against the live server (2026-08-10): `get_history`'s
`response.data.data` array carries one row per session, whether it has
finished or is still going. A row still playing or paused carries `id: null`
— Tautulli has not written a history row for it yet, so it is not history —
while a completed row's `id` is that row's own integer identity (e.g.
`35889`). `rating_key` and `user_id` arrive as JSON numbers, `machine_id` as
a string identical to the Plex device `clientIdentifier` it corresponds to;
`duration`, `paused_counter`, `percent_complete`, `ip_address`, and `stopped`
are all present on a completed row. `duration` is already net of paused time
— confirmed over 151 completed rows, `stopped - started - duration -
paused_counter` lands within 0-2 seconds of zero, rounding only.

`get_users` (issue #26) is shaped differently: `response.data` is the bare
list of user records itself, not `get_history`'s `{"data": [...]}`
DataTables wrapper — confirmed live (2026-08-10). Each record carries
`user_id` and `username`; `user_id 0` is Tautulli's own "Local" placeholder
for unauthenticated/local sessions, not a person.

This client hands back whatever the API returned, unfiltered and
uninterpreted — deciding what a null `id` or a missing hard key means is
`plays.match_tautulli_history`'s job, the same division `plex_client.py`
keeps from `plays.ingest_plays`.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from .errors import TautulliError

_DEFAULT_TIMEOUT = 30.0
#: Page size for `get_history`, which — like Plex's own history endpoint —
#: does not return everything in one response; the live server this was
#: measured against holds 35,889 rows.
_HISTORY_PAGE_SIZE = 1000


class TautulliSource(Protocol):
    """The read surface `plays.match_tautulli_history` needs from Tautulli —
    real or recorded."""

    def history(self) -> list[dict[str, Any]]:
        """Every history row Tautulli currently holds, completed and
        in-progress alike, in any order. The caller decides what a null
        `id` means; this method reports every row it is given."""
        ...


class TautulliUserSource(Protocol):
    """The read surface `enrich-tautulli-plays` needs from Tautulli to
    resolve the server owner's account id across systems (issue #26) — real
    or recorded, independent of `TautulliSource`'s history surface."""

    def users(self) -> list[dict[str, Any]]:
        """Every user Tautulli's `get_users` reports, including `user_id
        0`'s "Local" placeholder row."""
        ...


class LiveTautulliClient:
    """The one `TautulliSource`/`TautulliUserSource` that reaches a real
    Tautulli server, over `httpx`. Read-only by construction: the only calls
    this makes are `get_history` and `get_users`, and nothing here can
    mutate anything Tautulli holds.
    """

    def __init__(self, base_url: str, api_key: str, http: httpx.Client | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._http = http or httpx.Client(timeout=_DEFAULT_TIMEOUT)

    def history(self) -> list[dict[str, Any]]:
        """Every history row Tautulli holds, newest first, paginated via
        `start`/`length` the same way `curator.clients.tautulli.TautulliClient`
        does: a page shorter than requested is the last one, rather than
        trusting a `recordsFiltered` total that could shift while a long
        fetch is still paginating through it."""
        rows: list[dict[str, Any]] = []
        start = 0
        while True:
            data = self._call(
                "get_history",
                start=start,
                length=_HISTORY_PAGE_SIZE,
                order_column="date",
                order_dir="desc",
            )
            page: list[dict[str, Any]] = data.get("data", [])
            rows.extend(page)
            if len(page) < _HISTORY_PAGE_SIZE:
                break
            start += len(page)
        return rows

    def users(self) -> list[dict[str, Any]]:
        """Every user Tautulli holds via `get_users`, verbatim.

        Unlike `history()`, `get_users`'s `response.data` is the bare user
        list itself — no `{"data": [...]}` DataTables wrapper — so this does
        not reuse `history()`'s `data.get("data", [])` unwrap.
        """
        data = self._call("get_users")
        if isinstance(data, list):
            return data
        return []

    def _call(self, cmd: str, **params: Any) -> Any:
        """GET one Tautulli `cmd`, returning `response.data`.

        The key rides in the query string — Tautulli's `/api/v2` has no
        header-based auth — so, same reasoning as `tmdb_client.LiveTMDbClient._get`,
        every error message below is built from `cmd`/`resp.status_code`,
        never from `err` or `resp.url`: httpx's own `raise_for_status`
        formats its message as `"... for url '{response.url}'"`, and that URL
        carries the key. `from None`, not `from err`, severs the exception
        chain for the same reason one level deeper — a chained cause travels
        with the exception and would leave the key reachable through any
        traceback that prints one.
        """
        try:
            resp = self._http.get(
                f"{self._base}/api/v2",
                params={"apikey": self._key, "cmd": cmd, **params},
            )
        except httpx.HTTPError as err:
            raise TautulliError(
                f"cannot reach Tautulli at {self._base}: {type(err).__name__}"
            ) from None
        try:
            resp.raise_for_status()
        except httpx.HTTPError:
            raise TautulliError(f"Tautulli returned {resp.status_code} for cmd={cmd}") from None
        body: dict[str, Any] = resp.json()
        response: dict[str, Any] = body.get("response", {})
        if response.get("result") != "success":
            raise TautulliError(
                f"Tautulli reported failure for cmd={cmd}: {response.get('message')}"
            )
        return response.get("data", {})
