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
from plexdb.letterboxd_client import (
    FilmIds,
    ListContents,
    ListFilm,
    Lookup,
    Outcome,
    parse_film_page,
)

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


LIST_USER = "adaydreaming"
LIST_SLUG = "feeling-lost-in-your-20s"
LIST_URL = f"https://letterboxd.com/{LIST_USER}/list/{LIST_SLUG}"
#: The five films `list_page_1.html` (3) and `list_page_2.html` (2) hold, in list order.
LIST_FILMS = (
    ("scott-pilgrim-vs-the-world", "Scott Pilgrim vs. the World", 2010),
    ("millennium-mambo", "Millennium Mambo", 2001),
    ("the-devil-probably", "The Devil, Probably", 1977),
    ("me-and-earl-and-the-dying-girl", "Me and Earl and the Dying Girl", 2015),
    ("juno", "Juno", 2007),
)


def film_page(media_type: str = "movie", tmdb_id: str | None = INCEPTION_TMDB) -> str:
    """The recorded film page with its `<body>` identity replaced."""
    page = load("film_inception.html")
    marker = f'data-tmdb-type="movie" data-tmdb-id="{INCEPTION_TMDB}"'
    assert marker in page
    ids = f'data-tmdb-type="{media_type}"' + (f' data-tmdb-id="{tmdb_id}"' if tmdb_id else "")
    return page.replace(marker, ids)


@dataclass
class RecordedListSource:
    """A `LetterboxdListSource` over the recorded list pages.

    `ids` maps a film slug to its identity (a slug not in it fails like an unloadable film
    page). `failing` makes `list_contents` raise this error. `list_calls` and `film_calls`
    hold every ask in order.
    """

    ids: dict[str, FilmIds] = field(default_factory=dict)
    failing: LetterboxdError | None = None
    size: int | None = 5
    films: int = 5
    list_calls: list[tuple[str, str]] = field(default_factory=list)
    film_calls: list[str] = field(default_factory=list)

    def list_contents(self, user: str, slug: str) -> ListContents:
        self.list_calls.append((user, slug))
        if self.failing is not None:
            raise self.failing
        entries = tuple(ListFilm(s, t, y) for s, t, y in LIST_FILMS[: self.films])
        return ListContents("feeling lost in your 20's", self.size, entries, pages=2)

    def film_ids(self, slug: str) -> FilmIds:
        self.film_calls.append(slug)
        found = self.ids.get(slug)
        if found is None:
            raise LetterboxdError(f"/film/{slug}/ did not load")
        return found
