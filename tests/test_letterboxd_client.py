"""`LiveLetterboxdClient` against the recorded pages, through
`httpx.MockTransport` — never the live site."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from letterboxd_fixtures import INCEPTION_TMDB, load, markerless

from plexdb.errors import LetterboxdError
from plexdb.letterboxd_client import (
    BASE_URL,
    LiveLetterboxdClient,
    Outcome,
    disallowed_patterns,
    is_disallowed,
)

ROBOTS = disallowed_patterns(load("robots.txt"))


def _site(
    redirects: dict[str, str] | None = None,
    pages: dict[str, httpx.Response] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer robots.txt, then a redirect or page per path; anything else is 404."""
    redirects = {f"/tmdb/{INCEPTION_TMDB}/": "/film/inception/"} if redirects is None else redirects
    pages = (
        {"/film/inception/": httpx.Response(200, text=load("film_inception.html"))}
        if pages is None
        else pages
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text=load("robots.txt"))
        if path in redirects:
            return httpx.Response(302, headers={"Location": redirects[path]})
        return pages.get(path, httpx.Response(404))

    return handler


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> LiveLetterboxdClient:
    return LiveLetterboxdClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None
    )


def test_the_tmdb_redirect_reaches_the_film_page_and_reads_its_themes() -> None:
    client = _client(_site())

    found = client.lookup(INCEPTION_TMDB)

    assert found.outcome is Outcome.LISTED and found.path == "/film/inception/"
    assert found.themes[:2] == ("High speed and special ops", "Humanity and the world around us")
    assert len(found.themes) == 7
    assert client.requested == ["/robots.txt", "/tmdb/27205/", "/film/inception/"]


def test_no_request_goes_to_a_disallowed_path() -> None:
    client = _client(_site())
    client.lookup(INCEPTION_TMDB)
    client.lookup("999999999")

    assert client.requested.count("/robots.txt") == 1
    assert not [p for p in client.requested if is_disallowed(p, ROBOTS)]


def test_a_redirect_to_a_disallowed_path_is_never_followed() -> None:
    client = _client(_site(redirects={"/tmdb/1/": "/film/by/"}))

    with pytest.raises(LetterboxdError, match="robots.txt disallows /film/by/"):
        client.lookup("1")
    assert "/film/by/" not in client.requested


def test_a_redirect_somewhere_other_than_a_film_page_fails() -> None:
    client = _client(_site(redirects={"/tmdb/1/": "/films/theme/x/"}))

    with pytest.raises(LetterboxdError, match="not a film page"):
        client.lookup("1")


def test_the_import_result_page_means_not_listed() -> None:
    page = httpx.Response(200, text=load("tmdb_import_result.html"))
    client = _client(_site(pages={"/tmdb/999999999/": page}))

    assert client.lookup("999999999").outcome is Outcome.NOT_LISTED


def test_a_200_that_is_not_the_import_result_page_fails_rather_than_caching() -> None:
    page = httpx.Response(200, text="<html><title>Just a moment...</title></html>")
    client = _client(_site(pages={"/tmdb/999999999/": page}))

    with pytest.raises(LetterboxdError, match="without the TMDB Import Result page"):
        client.lookup("999999999")


def test_a_404_means_not_listed() -> None:
    assert _client(_site()).lookup("999999999").outcome is Outcome.NOT_LISTED


def test_a_film_page_without_the_marker_is_unparsed() -> None:
    page = httpx.Response(200, text=markerless(load("film_inception.html")))
    client = _client(_site(pages={"/film/inception/": page}))

    found = client.lookup(INCEPTION_TMDB)

    assert found.outcome is Outcome.UNPARSED and found.themes == ()


def test_a_renamed_themes_heading_is_unparsed_rather_than_an_empty_theme_set() -> None:
    html = load("film_inception.html").replace("<span>Themes</span>", "<span>Moods</span>")
    page = httpx.Response(200, text=html)
    client = _client(_site(pages={"/film/inception/": page}))

    found = client.lookup(INCEPTION_TMDB)

    assert found.outcome is Outcome.UNPARSED and found.themes == ()


def test_a_film_page_with_no_theme_links_is_listed_with_no_themes() -> None:
    html = load("film_inception.html").replace("<span>Themes</span>", "<span>Moods</span>")
    html = html.replace("/films/theme/", "/x/").replace("/films/mini-theme/", "/x/")
    page = httpx.Response(200, text=html)
    client = _client(_site(pages={"/film/inception/": page}))

    found = client.lookup(INCEPTION_TMDB)

    assert found.outcome is Outcome.LISTED and found.themes == ()


def test_a_marker_naming_another_film_is_unparsed() -> None:
    client = _client(_site(redirects={"/tmdb/1396/": "/film/inception/"}))

    assert client.lookup("1396").outcome is Outcome.UNPARSED


def test_a_cloudflare_challenge_fails_the_title() -> None:
    page = httpx.Response(403, text="Just a moment...")
    client = _client(_site(pages={"/film/inception/": page}))

    with pytest.raises(LetterboxdError, match="403"):
        client.lookup(INCEPTION_TMDB)


def test_a_429_backs_off_for_the_wait_it_asks_for_and_continues() -> None:
    answers = [httpx.Response(429, headers={"Retry-After": "7"})]
    site = _site()
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/film/inception/" and answers:
            return answers.pop(0)
        return site(request)

    client = LiveLetterboxdClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=waits.append
    )

    assert client.lookup(INCEPTION_TMDB).outcome is Outcome.LISTED
    assert 7.0 in waits


@pytest.mark.parametrize(
    "location", [f"{BASE_URL}/film/inception/", "http://letterboxd.com/film/inception/"]
)
def test_an_absolute_redirect_on_the_same_site_is_followed(location: str) -> None:
    client = _client(_site(redirects={f"/tmdb/{INCEPTION_TMDB}/": location}))

    assert client.lookup(INCEPTION_TMDB).outcome is Outcome.LISTED


def test_a_redirect_to_another_host_is_never_followed() -> None:
    client = _client(_site(redirects={"/tmdb/1/": "https://example.com/film/x/"}))

    with pytest.raises(LetterboxdError, match="not a film page"):
        client.lookup("1")
    assert client.requested == ["/robots.txt", "/tmdb/1/"]


def test_a_value_that_is_not_a_tmdb_id_is_refused() -> None:
    with pytest.raises(LetterboxdError, match="not a TMDB id"):
        _client(_site()).lookup("../film/by")


def test_the_generic_group_of_the_recorded_robots_is_read() -> None:
    assert "/*/genre/*" in ROBOTS and "/*/by/*" in ROBOTS
    assert is_disallowed("/films/theme/high-speed-and-special-ops/by/best-match/", ROBOTS)
    assert not is_disallowed("/tmdb/27205/", ROBOTS)
    assert not is_disallowed("/film/inception/", ROBOTS)
