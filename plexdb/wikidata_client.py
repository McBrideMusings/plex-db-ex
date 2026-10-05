"""Reading what Wikidata states about titles, by IMDb id, over its SPARQL
endpoint — read-only, one query per batch of titles.

`enrich_wikidata.py` depends on `WikidataSource`, not on `LiveWikidataClient`
directly, so a recorded-response test substitutes a fake without a socket —
the same split as `tmdb_client.py`/`TMDbSource`.

One query carries many IMDb ids through `VALUES` on P345 and asks for the
English labels of five properties at once, one result row per (IMDb id,
property, label). Checked against the live endpoint (2026-10-03): 400 ids in
one query answered in 4.9 s with 2,268 rows. `wdt:` reads only a statement's
best rank, so a deprecated narrative location never arrives. An IMDb id
Wikidata has no item for contributes no rows, which is how "no match" looks.

The query service's documented limits
(https://www.mediawiki.org/wiki/Wikidata_Query_Service/User_Manual#Query_limits)
are a 60 s deadline per query, 60 s of processing per minute per client and 5
parallel queries per IP. This client sends one query at a time, so processing
time can never exceed wall time, and it pauses between queries so a run of slow
queries stays below the per-minute budget rather than at it. A
429 carries `Retry-After`, which it honours a bounded number of times. Wikidata
blocks clients without a descriptive User-Agent
(https://meta.wikimedia.org/wiki/User-Agent_policy), so every request sends one.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol

import httpx

from . import __version__
from .errors import WikidataError

ENDPOINT = "https://query.wikidata.org/sparql"

#: Who is asking, and where to find out more — what the User-Agent policy asks for.
USER_AGENT = (
    f"plexdb/{__version__} (https://github.com/McBrideMusings/plex-db-ex; "
    "metadata store over a personal Plex library) httpx"
)

#: The five properties read, by Wikidata id.
NARRATIVE_LOCATION = "P840"
SET_IN_PERIOD = "P2408"
MAIN_SUBJECT = "P921"
GENRE = "P136"
AWARD_RECEIVED = "P166"
PROPERTIES = (NARRATIVE_LOCATION, SET_IN_PERIOD, MAIN_SUBJECT, GENRE, AWARD_RECEIVED)

_PROP_PREFIX = "http://www.wikidata.org/prop/direct/"

#: Past the service's own 60 s deadline, so its timeout error arrives as a
#: response rather than this client hanging up first.
_TIMEOUT = 65.0
#: Pause after every query. With one query in flight at a time this keeps the
#: client well under 60 s of processing per minute even when every query is slow.
_PAUSE_SECONDS = 1.0
#: 429s honoured for one batch before it counts as failed.
_MAX_RETRIES = 3
#: A `Retry-After` longer than this fails the batch rather than stalling a sweep.
_MAX_RETRY_AFTER = 120.0

#: One statement: (IMDb id, property id such as `P840`, English label).
Statement = tuple[str, str, str]


class WikidataSource(Protocol):
    """The read surface the Wikidata sweep needs — real or recorded."""

    def statements(self, imdb_ids: Sequence[str]) -> list[Statement]:
        """Every English-labelled value of `PROPERTIES` on the items carrying
        these IMDb ids, as one query. An id with no Wikidata item, or an item
        with none of the properties, contributes nothing."""
        ...


def is_imdb_id(value: str) -> bool:
    """A title's IMDb id: `tt` and digits. Nothing else is spliced into a query."""
    return value.startswith("tt") and value[2:].isdigit()


def build_query(imdb_ids: Sequence[str]) -> str:
    """The SPARQL for one batch. A value that is not an IMDb id is refused here
    rather than spliced into the query text; the sweep filters them out first."""
    for imdb_id in imdb_ids:
        if not is_imdb_id(imdb_id):
            raise WikidataError(f"not an IMDb id: {imdb_id!r}")
    values = " ".join(f'"{imdb_id}"' for imdb_id in imdb_ids)
    props = " ".join(f"wdt:{prop}" for prop in PROPERTIES)
    return (
        "SELECT ?imdb ?prop ?label WHERE {\n"
        f"  VALUES ?imdb {{ {values} }}\n"
        f"  VALUES ?prop {{ {props} }}\n"
        "  ?film wdt:P345 ?imdb ; ?prop ?value .\n"
        "  ?value rdfs:label ?label .\n"
        '  FILTER(LANG(?label) = "en")\n'
        "}"
    )


def parse_results(body: dict[str, Any]) -> list[Statement]:
    """SPARQL JSON results → statements. A row missing a binding is dropped."""
    out: list[Statement] = []
    for binding in body.get("results", {}).get("bindings", []):
        try:
            imdb_id = binding["imdb"]["value"]
            prop_uri = binding["prop"]["value"]
            label = binding["label"]["value"]
        except (KeyError, TypeError):
            continue
        if prop_uri.startswith(_PROP_PREFIX):
            out.append((imdb_id, prop_uri.removeprefix(_PROP_PREFIX), label))
    return out


class LiveWikidataClient:
    """The one `WikidataSource` that reaches query.wikidata.org, over `httpx`."""

    def __init__(
        self,
        http: httpx.Client | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http or httpx.Client(timeout=_TIMEOUT)
        self._sleep = sleep

    def statements(self, imdb_ids: Sequence[str]) -> list[Statement]:
        if not imdb_ids:
            return []
        query = build_query(imdb_ids)
        for attempt in range(_MAX_RETRIES + 1):
            started = time.monotonic()
            try:
                resp = self._http.post(
                    ENDPOINT,
                    data={"query": query},
                    headers={
                        "Accept": "application/sparql-results+json",
                        "User-Agent": USER_AGENT,
                    },
                )
            except httpx.HTTPError as err:
                raise WikidataError(
                    f"cannot reach Wikidata at {ENDPOINT}: {type(err).__name__}"
                ) from None
            elapsed = time.monotonic() - started
            if resp.status_code == 429:
                if attempt == _MAX_RETRIES:
                    break
                wait = _retry_after(resp)
                print(f"wikidata: 429 after {elapsed:.1f} s, waiting {wait:.0f} s", flush=True)
                self._sleep(wait)
                continue
            self._sleep(_PAUSE_SECONDS)
            if resp.status_code != 200:
                raise WikidataError(
                    f"Wikidata returned {resp.status_code} for a {len(imdb_ids)}-title query "
                    f"after {elapsed:.1f} s"
                )
            try:
                body: dict[str, Any] = resp.json()
            except ValueError:
                raise WikidataError("Wikidata returned a response that is not JSON") from None
            statements = parse_results(body)
            print(
                f"wikidata: {len(imdb_ids)} id(s) -> {len(statements)} statement(s) "
                f"in {elapsed:.1f} s",
                flush=True,
            )
            return statements
        raise WikidataError(f"Wikidata kept answering 429 after {_MAX_RETRIES} retries")


def _retry_after(resp: httpx.Response) -> float:
    raw = resp.headers.get("Retry-After", "").strip()
    try:
        wait = float(raw)
    except ValueError:
        wait = 60.0
    if math.isnan(wait):
        wait = 60.0
    if wait > _MAX_RETRY_AFTER:
        raise WikidataError(f"Wikidata asked for a {wait:.0f} s wait; giving up on this batch")
    return max(wait, 1.0)
