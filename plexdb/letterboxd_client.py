"""Reading the Themes section of a film's Letterboxd page, by TMDB movie id —
read-only, keyless, and scraped, because Letterboxd has no public API for
individuals.

`enrich_letterboxd.py` depends on `LetterboxdSource`, not on
`LiveLetterboxdClient` directly, so a recorded-response test substitutes a fake
without a socket — the same split as `wikidata_client.py`/`WikidataSource`.

Two requests per title, checked against the live site on 2026-10-04:

- `GET /tmdb/<id>/` answers 302 to the film page (`/tmdb/27205/` →
  `/film/inception/`). An id Letterboxd has no film for answers **200** with a
  "TMDB Import Result" page, not 404; either one means *not listed*. A 200
  without that page fails the title instead, so a challenge page answering 200
  is never cached as an absent film.
- `GET /film/<slug>/` answers the film page. Its `<body>` carries
  `data-tmdb-id`, the film marker: a page whose marker is missing or names
  another id is *unparsed*, which is how a layout change looks. The Genres tab
  holds a `Themes` heading over up to two `/films/theme/` links and five
  `/films/mini-theme/` links; both kinds are themes here. The "Show All…" page
  behind them (`/film/<slug>/themes/`) answers a Cloudflare challenge (403) to a
  client that runs no JavaScript, so the film page's subset is what there is.

Every request path is checked against the `Disallow` rules robots.txt gives
`User-agent: *`, fetched once per client, and a disallowed path raises before
anything is sent. The theme links themselves end in `/by/best-match/`, which
those rules disallow; they are read as text and never followed.

The client pauses `_PAUSE_SECONDS` after every request, so one title costs two
requests and a little over two seconds. A 429 carries `Retry-After`, honoured a
bounded number of times; a 403 is a Cloudflare challenge and fails the title.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from html.parser import HTMLParser
from typing import Protocol

import httpx

from . import __version__
from .errors import LetterboxdError

BASE_URL = "https://letterboxd.com"
_HOST = "letterboxd.com"
#: The `<title>` text of the page `/tmdb/<id>/` answers for an id with no film.
_IMPORT_RESULT_TITLE = "TMDB Import Result"
ROBOTS_PATH = "/robots.txt"

#: Who is asking, and where to find out more.
USER_AGENT = (
    f"plexdb/{__version__} (https://github.com/McBrideMusings/plex-db-ex; "
    "metadata store over a personal Plex library) httpx"
)

_TIMEOUT = 30.0
#: Pause after every request; two requests per title.
_PAUSE_SECONDS = 1.0
#: 429s honoured for one request before the title counts as failed.
_MAX_RETRIES = 3
#: A `Retry-After` longer than this fails the title rather than stalling a sweep.
_MAX_RETRY_AFTER = 120.0

#: The only shape a redirect from `/tmdb/<id>/` is followed to.
_FILM_PATH = re.compile(r"^/film/[^/?#]+/$")
_THEME_HREF = re.compile(r"^/films/(?:theme|mini-theme)/")


class Outcome(Enum):
    """What one title's lookup found."""

    #: The film page loaded and carried the marker; `themes` may still be empty.
    LISTED = "listed"
    #: `/tmdb/<id>/` did not redirect to a film page: Letterboxd has no such film.
    NOT_LISTED = "not_listed"
    #: The film page loaded without a `data-tmdb-id` matching the id asked, or
    #: with theme links the `Themes` heading does not lead to.
    UNPARSED = "unparsed"


@dataclass(frozen=True)
class Lookup:
    outcome: Outcome
    #: The film page's path, when `/tmdb/<id>/` redirected to one.
    path: str | None = None
    #: Theme and mini-theme labels as the page shows them, in page order.
    themes: tuple[str, ...] = field(default_factory=tuple)


class LetterboxdSource(Protocol):
    """The read surface the Letterboxd sweep needs — real or recorded."""

    def lookup(self, tmdb_id: str) -> Lookup:
        """Find this TMDB movie id's film page and read its themes. Raises
        `LetterboxdError` when a request fails; a missing film or a page
        without the marker is an outcome, not an error."""
        ...


def is_tmdb_id(value: str) -> bool:
    """A TMDB id: digits only. Nothing else is spliced into a request path."""
    return value.isascii() and value.isdigit()


def disallowed_patterns(robots_txt: str) -> list[str]:
    """The `Disallow` values of every group naming `User-agent: *`.

    `Allow` lines are ignored, which can only make this client stricter than
    the file. An empty `Disallow` allows everything and adds nothing.
    """
    patterns: list[str] = []
    agents: list[str] = []
    in_rules = False
    for raw in robots_txt.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        name, value = (part.strip() for part in line.split(":", 1))
        name = name.lower()
        if name == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(value)
        elif name in ("allow", "disallow"):
            in_rules = True
            if name == "disallow" and value and "*" in agents:
                patterns.append(value)
    return patterns


def is_disallowed(path: str, patterns: list[str]) -> bool:
    """Whether `path` matches any robots.txt pattern: `*` matches any run of
    characters, a trailing `$` anchors the end, and every pattern anchors the
    start."""
    for pattern in patterns:
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        regex = ".*".join(re.escape(part) for part in body.split("*"))
        if re.match(regex + ("$" if anchored else ""), path):
            return True
    return False


class _FilmPageParser(HTMLParser):
    """Collects the `<body>` film marker and the links under the `Themes` heading."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tmdb_id: str | None = None
        self.themes: list[str] = []
        #: Theme links anywhere on the page, under the `Themes` heading or not.
        self.theme_links = 0
        self._h3_text: list[str] | None = None
        self._after_themes_heading = False
        self._in_sluglist = False
        self._div_depth = 0
        self._link_text: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "body":
            self.tmdb_id = attr.get("data-tmdb-id")
        elif tag == "h3":
            self._h3_text = []
            self._after_themes_heading = False
        elif tag == "div":
            if self._in_sluglist:
                self._div_depth += 1
            elif self._after_themes_heading and "text-sluglist" in (attr.get("class") or ""):
                self._in_sluglist = True
                self._div_depth = 1
        elif tag == "a" and _THEME_HREF.match(attr.get("href") or ""):
            self.theme_links += 1
            if self._in_sluglist:
                self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "h3" and self._h3_text is not None:
            self._after_themes_heading = "".join(self._h3_text).strip() == "Themes"
            self._h3_text = None
        elif tag == "a" and self._link_text is not None:
            label = " ".join("".join(self._link_text).split())
            if label:
                self.themes.append(label)
            self._link_text = None
        elif tag == "div" and self._in_sluglist:
            self._div_depth -= 1
            if self._div_depth == 0:
                self._in_sluglist = False
                self._after_themes_heading = False

    def handle_data(self, data: str) -> None:
        if self._h3_text is not None:
            self._h3_text.append(data)
        if self._link_text is not None:
            self._link_text.append(data)


def parse_film_page(html: str, tmdb_id: str, path: str | None = None) -> Lookup:
    """A film page → its themes, or `UNPARSED` when the marker is missing or
    names another id, or when theme links sit on the page but none under the
    `Themes` heading — a renamed heading or list, which would otherwise cache
    an empty theme set. A film with no themes has no theme links at all."""
    parser = _FilmPageParser()
    parser.feed(html)
    parser.close()
    if parser.tmdb_id != tmdb_id or (parser.theme_links and not parser.themes):
        return Lookup(Outcome.UNPARSED, path=path)
    return Lookup(Outcome.LISTED, path=path, themes=tuple(dict.fromkeys(parser.themes)))


class LiveLetterboxdClient:
    """The one `LetterboxdSource` that reaches letterboxd.com, over `httpx`."""

    def __init__(
        self,
        http: httpx.Client | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http or httpx.Client(timeout=_TIMEOUT)
        self._sleep = sleep
        self._disallowed: list[str] | None = None
        #: Every path this client sent a request for, in order.
        self.requested: list[str] = []

    def lookup(self, tmdb_id: str) -> Lookup:
        if not is_tmdb_id(tmdb_id):
            raise LetterboxdError(f"not a TMDB id: {tmdb_id!r}")
        started = time.monotonic()
        resp = self._get(f"/tmdb/{tmdb_id}/")
        if resp.status_code == 404 or (
            resp.status_code == 200 and _IMPORT_RESULT_TITLE in resp.text
        ):
            print(
                f"letterboxd: tmdb {tmdb_id} -> not listed ({resp.status_code}) "
                f"in {time.monotonic() - started:.1f} s",
                flush=True,
            )
            return Lookup(Outcome.NOT_LISTED)
        if resp.status_code not in (301, 302, 303, 307, 308):
            # A 200 that is not the import-result page is something in front of
            # the site, not an answer about this film; caching it as "not
            # listed" would hide every title for a whole staleness window.
            raise LetterboxdError(
                f"Letterboxd returned {resp.status_code} for tmdb {tmdb_id}"
                + (" without the TMDB Import Result page" if resp.status_code == 200 else "")
            )
        location = resp.headers.get("Location", "")
        target = httpx.URL(BASE_URL).join(location)
        path = target.path if target.host == _HOST else location
        if not _FILM_PATH.match(path):
            raise LetterboxdError(
                f"Letterboxd redirected tmdb {tmdb_id} to {location!r}, not a film page"
            )
        page = self._get(path)
        if page.status_code != 200:
            raise LetterboxdError(f"Letterboxd returned {page.status_code} for {path}")
        found = parse_film_page(page.text, tmdb_id, path)
        detail = (
            f"{len(found.themes)} theme(s)"
            if found.outcome is Outcome.LISTED
            else "no film marker for this id"
        )
        print(
            f"letterboxd: tmdb {tmdb_id} -> {path} {detail} in {time.monotonic() - started:.1f} s",
            flush=True,
        )
        return found

    def _robots(self) -> list[str]:
        if self._disallowed is None:
            resp = self._send(ROBOTS_PATH)
            if resp.status_code != 200:
                raise LetterboxdError(f"Letterboxd returned {resp.status_code} for robots.txt")
            self._disallowed = disallowed_patterns(resp.text)
            print(
                f"letterboxd: robots.txt disallows {len(self._disallowed)} pattern(s) "
                "for User-agent: *",
                flush=True,
            )
        return self._disallowed

    def _get(self, path: str) -> httpx.Response:
        if is_disallowed(path, self._robots()):
            raise LetterboxdError(f"robots.txt disallows {path}; not requesting it")
        return self._send(path)

    def _send(self, path: str) -> httpx.Response:
        for attempt in range(_MAX_RETRIES + 1):
            self.requested.append(path)
            try:
                resp = self._http.get(
                    BASE_URL + path,
                    headers={"User-Agent": USER_AGENT},
                    follow_redirects=False,
                )
            except httpx.HTTPError as err:
                raise LetterboxdError(
                    f"cannot reach Letterboxd at {BASE_URL}{path}: {type(err).__name__}"
                ) from None
            if resp.status_code == 429 and attempt < _MAX_RETRIES:
                wait = _retry_after(resp)
                print(f"letterboxd: 429 on {path}, waiting {wait:.0f} s", flush=True)
                self._sleep(wait)
                continue
            self._sleep(_PAUSE_SECONDS)
            if resp.status_code == 429:
                break
            if resp.status_code == 403:
                raise LetterboxdError(
                    f"Letterboxd answered 403 for {path} (a Cloudflare challenge)"
                )
            return resp
        raise LetterboxdError(f"Letterboxd kept answering 429 after {_MAX_RETRIES} retries")


def _retry_after(resp: httpx.Response) -> float:
    raw = resp.headers.get("Retry-After", "").strip()
    try:
        wait = float(raw)
    except ValueError:
        wait = 60.0
    if math.isnan(wait):
        wait = 60.0
    if wait > _MAX_RETRY_AFTER:
        raise LetterboxdError(f"Letterboxd asked for a {wait:.0f} s wait; giving up on this title")
    return max(wait, 1.0)
