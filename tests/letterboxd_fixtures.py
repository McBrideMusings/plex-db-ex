"""A `LetterboxdSource` built from the recorded pages in `fixtures/letterboxd/`.

All three were fetched from letterboxd.com on 2026-10-04 with the client's
User-Agent: `film_inception.html` is `/film/inception/`, where `/tmdb/27205/`
redirects; `tmdb_import_result.html` is the 200 `/tmdb/999999999/` answers for
an id Letterboxd has no film for; `robots.txt` is the site's robots file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from plexdb.errors import LetterboxdError
from plexdb.letterboxd_client import Lookup, Outcome, parse_film_page

FIXTURES = Path(__file__).parent / "fixtures" / "letterboxd"
INCEPTION_TMDB = "27205"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def markerless(html: str) -> str:
    """The recorded page with its film marker removed — what a layout change looks like."""
    return html.replace(f'data-tmdb-id="{INCEPTION_TMDB}"', "")


@dataclass
class RecordedLetterboxdSource:
    """Answers from the recorded Inception page by TMDB id.

    `pages` maps a TMDB id to the film page HTML its redirect would load; an id
    not in it is not listed. `calls` holds every id asked, in order, and
    `fail_calls` holds 1-indexed call numbers that raise instead of answering.
    """

    pages: dict[str, str] = field(
        default_factory=lambda: {INCEPTION_TMDB: load("film_inception.html")}
    )
    fail_calls: set[int] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    def lookup(self, tmdb_id: str) -> Lookup:
        self.calls.append(tmdb_id)
        if len(self.calls) in self.fail_calls:
            raise LetterboxdError(f"scripted failure on call {len(self.calls)}")
        html = self.pages.get(tmdb_id)
        if html is None:
            return Lookup(Outcome.NOT_LISTED)
        return parse_film_page(html, tmdb_id, f"/film/{tmdb_id}/")
