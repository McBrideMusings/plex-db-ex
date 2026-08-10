"""`LiveTMDbClient` against recorded responses, never a live server.

`tests/fixtures/tmdb/*.json` were captured from the real TMDB API's
`/movie/{id}/keywords` and `/tv/{id}/keywords` endpoints (verified live,
2026-08-10) — `httpx.MockTransport` serves them back over the exact
request-building and JSON-parsing code `LiveTMDbClient` uses against a real
server, so this is a genuine test of the adapter, just with no socket.
"""

from __future__ import annotations

import httpx
import pytest
from tmdb_fixtures import load

from plexdb.errors import TMDbError
from plexdb.tmdb_client import LiveTMDbClient

#: request path -> recorded fixture file, mirroring what the live API
#: actually returned for these ids.
_RECORDED = {
    "/3/movie/155/keywords": "movie_155_keywords.json",
    "/3/tv/1396/keywords": "tv_1396_keywords.json",
    "/3/movie/155/recommendations": "movie_155_recommendations.json",
    "/3/movie/155/similar": "movie_155_similar.json",
    "/3/tv/1396/recommendations": "tv_1396_recommendations.json",
    "/3/tv/1396/similar": "tv_1396_similar.json",
    "/3/movie/424783/recommendations": "movie_zero_recommendations.json",
}


def _handler(request: httpx.Request) -> httpx.Response:
    assert request.url.params["api_key"] == "the-test-key"
    fixture = _RECORDED.get(request.url.path)
    if fixture is None:
        # Recorded live: TMDB's 404 for an id it has never heard of.
        return httpx.Response(404, json=load("movie_not_found_404.json"))
    return httpx.Response(200, json=load(fixture))


def _client() -> LiveTMDbClient:
    http = httpx.Client(transport=httpx.MockTransport(_handler))
    return LiveTMDbClient("the-test-key", http=http)


def test_movie_keywords_come_from_the_keywords_array() -> None:
    names = _client().keywords("155", "movie")

    assert len(names) == 18
    assert "superhero" in names


def test_tv_keywords_come_from_the_results_array() -> None:
    names = _client().keywords("1396", "tv")

    assert len(names) == 28
    assert "drug dealer" in names


def test_an_id_tmdb_has_never_heard_of_returns_no_keywords_not_an_error() -> None:
    names = _client().keywords("999999999", "movie")

    assert names == []


def test_movie_recommendations_come_back_as_ordered_tmdb_ids() -> None:
    ids = _client().recommendations("155", "movie")

    assert len(ids) == 20
    assert ids[0] == "268"  # Batman (1989) — TMDB's own top recommendation
    assert all(isinstance(i, str) for i in ids)


def test_tv_recommendations_come_back_as_ordered_tmdb_ids() -> None:
    ids = _client().recommendations("1396", "tv")

    assert len(ids) == 20
    assert ids[0] == "60059"  # Better Call Saul


def test_movie_similar_comes_back_as_ordered_tmdb_ids() -> None:
    ids = _client().similar("155", "movie")

    assert len(ids) == 20
    assert ids[0] == "29764"


def test_tv_similar_comes_back_as_ordered_tmdb_ids() -> None:
    ids = _client().similar("1396", "tv")

    assert len(ids) == 20
    assert ids[0] == "89"  # Titus


def test_an_id_tmdb_has_never_heard_of_returns_no_recommendations_not_an_error() -> None:
    ids = _client().recommendations("999999999", "movie")

    assert ids == []


def test_a_title_with_zero_recommendations_returns_an_empty_list() -> None:
    ids = _client().recommendations("424783", "movie")

    assert ids == []


def test_an_unreachable_server_raises_tmdb_error_not_a_raw_httpx_error() -> None:
    def _broken(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    http = httpx.Client(transport=httpx.MockTransport(_broken))
    client = LiveTMDbClient("the-test-key", http=http)

    with pytest.raises(TMDbError, match="cannot reach TMDB"):
        client.keywords("155", "movie")


def test_a_non_404_error_response_raises_tmdb_error() -> None:
    def _unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"status_message": "Invalid API key"})

    http = httpx.Client(transport=httpx.MockTransport(_unauthorized))
    client = LiveTMDbClient("bad-key", http=http)

    with pytest.raises(TMDbError, match="TMDB returned 401"):
        client.keywords("155", "movie")


def test_an_error_response_never_leaks_the_api_key_into_the_raised_message() -> None:
    # httpx's own `raise_for_status()` formats its message as
    # "... for url '<response.url>'", and that URL carries the query string
    # — so stringifying the raw httpx exception would print a real API key
    # to stderr on every 401/429/5xx TMDB returns. This is the regression
    # test for that leak.
    def _unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"status_message": "Invalid API key"})

    http = httpx.Client(transport=httpx.MockTransport(_unauthorized))
    client = LiveTMDbClient("the-real-secret-key", http=http)

    with pytest.raises(TMDbError) as exc_info:
        client.keywords("155", "movie")

    assert "the-real-secret-key" not in str(exc_info.value)


def test_the_api_key_never_reaches_a_traceback() -> None:
    """The message was already clean; the exception *chain* was not.

    `raise ... from err` keeps httpx's own error as `__cause__`, and httpx
    formats its message with the full request URL — query string included. So
    the key stayed reachable through any traceback: an unhandled crash, a
    logger with `exc_info`, a CI failure dump. Severing the chain is what keeps
    it out, and only a traceback-level assertion catches a regression.
    """
    import traceback

    key = "SECRET-KEY-DO-NOT-LEAK"
    http = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(401, json={}))
    )
    client = LiveTMDbClient(key, http=http)

    try:
        client.keywords("155", "movie")
    except TMDbError:
        rendered = traceback.format_exc()
    else:  # pragma: no cover - the 401 above always raises
        raise AssertionError("expected TMDbError")

    assert key not in rendered
    assert "api_key=" not in rendered
