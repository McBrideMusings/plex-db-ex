"""`LivePlexClient` against recorded responses, never a live server.

`tests/fixtures/plex/*.json` were captured from a real Plex server's
`/library/sections` and `/library/sections/<key>/all` endpoints, trimmed to the
fields this client reads. `httpx.MockTransport`
serves them back over the exact request-building and JSON-parsing code
`LivePlexClient` uses against a real server, so this is a genuine test of the
adapter — just with no socket.
"""

from __future__ import annotations

import httpx
import pytest
from plex_fixtures import load

from plexdb import plex_client
from plexdb.errors import PlexError
from plexdb.plex_client import (
    PLEX_TYPE_EPISODE,
    PLEX_TYPE_MOVIE,
    PLEX_TYPE_SHOW,
    LivePlexClient,
)

#: request path+query -> recorded fixture file, mirroring what the live
#: server actually returned for these section/type combinations.
_RECORDED = {
    ("/library/sections", ""): "sections.json",
    (
        "/library/sections/1/all",
        "type=1&includeGuids=1&X-Plex-Container-Start=0&X-Plex-Container-Size=500",
    ): "section_1_type_1.json",
    (
        "/library/sections/2/all",
        "type=2&includeGuids=1&X-Plex-Container-Start=0&X-Plex-Container-Size=500",
    ): "section_2_type_2.json",
    (
        "/library/sections/2/all",
        "type=4&includeGuids=1&X-Plex-Container-Start=0&X-Plex-Container-Size=500",
    ): "section_2_type_4.json",
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


def test_collections_lists_every_collection_a_section_reports() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/library/sections/1/collections"
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Metadata": [
                        {"ratingKey": "500", "title": "Batman", "smart": None, "childCount": 2},
                        {
                            "ratingKey": "501",
                            "title": "Recently Released Movies",
                            "smart": "1",
                            "childCount": 30,
                        },
                    ]
                }
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(_handler))
    client = LivePlexClient("http://plex.example:32400", "t", http=http)

    collections = client.collections("1")

    assert [(c["ratingKey"], c["title"], c.get("smart")) for c in collections] == [
        ("500", "Batman", None),
        ("501", "Recently Released Movies", "1"),
    ]


def test_collection_children_lists_every_member_of_one_collection() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/library/metadata/500/children"
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Metadata": [
                        {"ratingKey": "100", "title": "The Dark Knight", "type": "movie"},
                        {"ratingKey": "101", "title": "Batman Begins", "type": "movie"},
                    ]
                }
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(_handler))
    client = LivePlexClient("http://plex.example:32400", "t", http=http)

    children = client.collection_children("500")

    assert [c["title"] for c in children] == ["The Dark Knight", "Batman Begins"]


def test_accounts_lists_every_account_including_the_id_zero_placeholder() -> None:
    """Confirmed live (2026-08-10): `/accounts` returns `MediaContainer.Account`
    with `id` and `name`; id 0 is a placeholder with an empty `name` on the
    live server. The ids and names here are synthetic: an account list is personal data."""

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/accounts"
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Account": [
                        {"id": 0, "name": ""},
                        {"id": 1, "name": "server-owner"},
                        {"id": 4242424, "name": "shared-user"},
                    ]
                }
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(_handler))
    client = LivePlexClient("http://plex.example:32400", "t", http=http)

    accounts = client.accounts()

    assert [(a["id"], a["name"]) for a in accounts] == [
        (0, ""),
        (1, "server-owner"),
        (4242424, "shared-user"),
    ]


def test_items_pages_a_section_and_joins_to_the_unpaged_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(plex_client, "_SECTION_PAGE_SIZE", 2)
    everything = [{"ratingKey": str(n), "title": f"episode {n}"} for n in range(5)]
    starts: list[int] = []

    def _paged(request: httpx.Request) -> httpx.Response:
        assert request.url.params["X-Plex-Container-Size"] == "2"
        assert request.url.params["type"] == "4"
        start = int(request.url.params["X-Plex-Container-Start"])
        starts.append(start)
        return httpx.Response(
            200,
            json={"MediaContainer": {"totalSize": 5, "Metadata": everything[start : start + 2]}},
        )

    http = httpx.Client(transport=httpx.MockTransport(_paged))
    client = LivePlexClient("http://plex.example:32400", "t", http=http)

    assert client.items("2", PLEX_TYPE_EPISODE) == everything
    assert starts == [0, 2, 4]


def test_items_keeps_paging_past_a_short_page_while_total_size_says_more() -> None:
    everything = [{"ratingKey": str(n)} for n in range(4)]

    def _capped(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["X-Plex-Container-Start"])
        # The server hands back at most one row per request, whatever size was asked.
        return httpx.Response(
            200,
            json={"MediaContainer": {"totalSize": 4, "Metadata": everything[start : start + 1]}},
        )

    http = httpx.Client(transport=httpx.MockTransport(_capped))
    client = LivePlexClient("http://plex.example:32400", "t", http=http)

    assert client.items("2", PLEX_TYPE_EPISODE) == everything


def test_a_200_with_a_non_json_body_raises_plex_error_not_a_json_traceback() -> None:
    # A proxy or captive portal in front of Plex can answer 200 with an HTML
    # page instead of the JSON Plex normally returns — issue #49.
    def _html_interstitial(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<!DOCTYPE html><title>Just a moment...</title>",
            headers={"content-type": "text/html"},
        )

    http = httpx.Client(transport=httpx.MockTransport(_html_interstitial))
    client = LivePlexClient("http://plex.example:32400", "the-test-token", http=http)

    with pytest.raises(PlexError, match="not JSON"):
        client.sections()


def test_a_200_with_valid_json_that_is_not_an_object_raises_plex_error() -> None:
    # A body that parses as JSON but isn't the expected object (a bare list,
    # a string, null) must not fall through to AttributeError on .get() — #49.
    def _bare_list(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    http = httpx.Client(transport=httpx.MockTransport(_bare_list))
    client = LivePlexClient("http://plex.example:32400", "the-test-token", http=http)

    with pytest.raises(PlexError, match="expected an object"):
        client.sections()
