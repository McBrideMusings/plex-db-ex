"""`LivePlexClient` against recorded responses, never a live server.

`tests/fixtures/plex/*.json` were captured from a real Plex server's
`/library/sections` and `/library/sections/<key>/all` endpoints, trimmed to
the fields this client reads. `httpx.MockTransport` serves them back over
the exact request-building and JSON-parsing code `LivePlexClient` uses
against a real server, so this is a genuine test of the adapter — just with
no socket.
"""

from __future__ import annotations

import httpx
import pytest
from plex_fixtures import load

from plexdb.errors import PlexError
from plexdb.plex_client import PLEX_TYPE_EPISODE, PLEX_TYPE_MOVIE, PLEX_TYPE_SHOW, LivePlexClient

#: request path+query -> recorded fixture file, mirroring what the live
#: server actually returned for these section/type combinations.
_RECORDED = {
    ("/library/sections", ""): "sections.json",
    ("/library/sections/1/all", "type=1&includeGuids=1"): "section_1_type_1.json",
    ("/library/sections/2/all", "type=2&includeGuids=1"): "section_2_type_2.json",
    ("/library/sections/2/all", "type=4&includeGuids=1"): "section_2_type_4.json",
}


def _handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["X-Plex-Token"] == "the-test-token"
    assert request.headers["Accept"] == "application/json"
    key = (request.url.path, request.url.query.decode())
    fixture = _RECORDED.get(key)
    if fixture is None:
        return httpx.Response(404, json={"error": f"no recording for {key}"})
    return httpx.Response(200, json=load(fixture))


def _client() -> LivePlexClient:
    http = httpx.Client(transport=httpx.MockTransport(_handler))
    return LivePlexClient("http://plex.example:32400", "the-test-token", http=http)


def test_sections_lists_all_four_real_section_shapes() -> None:
    sections = _client().sections()

    assert [(s.key, s.type, s.title) for s in sections] == [
        ("1", "movie", "Movies"),
        ("2", "show", "TV Shows"),
        ("4", "movie", "Concerts"),
        ("3", "movie", "Power Hours"),
    ]


def test_items_fetches_movies_with_guids_included() -> None:
    items = _client().items("1", PLEX_TYPE_MOVIE)

    titles = [item["title"] for item in items]
    assert titles == ["The 'Burbs", "Air Mater"]
    burbs = items[0]
    assert [g["id"] for g in burbs["Guid"]] == [
        "imdb://tt0096734",
        "tmdb://11974",
        "tvdb://5869",
    ]


def test_items_fetches_show_level_and_episode_level_records_separately() -> None:
    client = _client()

    shows = client.items("2", PLEX_TYPE_SHOW)
    episodes = client.items("2", PLEX_TYPE_EPISODE)

    assert [s["title"] for s in shows] == ["1923"]
    assert [e["title"] for e in episodes] == [
        "1923",
        "Nature's Empty Throne",
        "The Killing Season",
    ]
    # Two seasons are represented, and every episode traces back to the show.
    assert {e["parentIndex"] for e in episodes} == {1, 2}
    assert {e["grandparentRatingKey"] for e in episodes} == {"81044"}


def test_an_unreachable_server_raises_plex_error_not_a_raw_httpx_error() -> None:
    def _broken(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    http = httpx.Client(transport=httpx.MockTransport(_broken))
    client = LivePlexClient("http://plex.example:32400", "t", http=http)

    with pytest.raises(PlexError, match="cannot reach Plex"):
        client.sections()


def test_a_non_200_response_raises_plex_error() -> None:
    def _not_found(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    http = httpx.Client(transport=httpx.MockTransport(_not_found))
    client = LivePlexClient("http://plex.example:32400", "bad-token", http=http)

    with pytest.raises(PlexError, match="cannot reach Plex"):
        client.sections()
