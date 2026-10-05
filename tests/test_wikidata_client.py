"""`LiveWikidataClient` against the recorded SPARQL response, through
`httpx.MockTransport` — never the live endpoint."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest
from wikidata_fixtures import load

from plexdb.errors import WikidataError
from plexdb.wikidata_client import ENDPOINT, USER_AGENT, LiveWikidataClient, build_query


def _client(handler: httpx.MockTransport, waits: list[float]) -> LiveWikidataClient:
    return LiveWikidataClient(httpx.Client(transport=handler), sleep=waits.append)


def test_one_query_carries_every_id_and_a_descriptive_user_agent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=load())

    statements = _client(httpx.MockTransport(handler), []).statements(
        ["tt1375666", "tt0120815", "tt0000000"]
    )

    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == ENDPOINT
    assert request.headers["User-Agent"] == USER_AGENT
    assert "github.com/McBrideMusings/plex-db-ex" in USER_AGENT
    query = parse_qs(request.content.decode())["query"][0]
    assert query == build_query(["tt1375666", "tt0120815", "tt0000000"])
    assert ("tt1375666", "P840", "Los Angeles") in statements
    assert ("tt0120815", "P2408", "1940s") in statements
    assert not [s for s in statements if s[0] == "tt0000000"]


def test_a_429_is_retried_after_the_wait_it_asks_for() -> None:
    answers = [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json=load())]
    waits: list[float] = []

    statements = _client(httpx.MockTransport(lambda r: answers.pop(0)), waits).statements(
        ["tt1375666"]
    )

    assert waits[0] == 7.0
    assert statements


def test_a_retry_after_that_is_not_a_number_waits_a_minute() -> None:
    answers = [
        httpx.Response(429, headers={"Retry-After": "nan"}),
        httpx.Response(200, json=load()),
    ]
    waits: list[float] = []

    _client(httpx.MockTransport(lambda r: answers.pop(0)), waits).statements(["tt1375666"])

    assert waits[0] == 60.0


def test_a_server_error_raises_without_retrying() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, text="java.util.concurrent.TimeoutException")

    with pytest.raises(WikidataError, match="returned 500"):
        _client(httpx.MockTransport(handler), []).statements(["tt1375666"])
    assert calls == [1]


def test_a_value_that_is_not_an_imdb_id_never_reaches_the_query() -> None:
    with pytest.raises(WikidataError, match="not an IMDb id"):
        build_query(['tt1" } ?x ?y ?z { "'])


def test_exhausted_429s_raise_without_waiting_after_the_last_one() -> None:
    waits: list[float] = []
    client = _client(
        httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "5"})), waits
    )

    with pytest.raises(WikidataError, match="kept answering 429"):
        client.statements(["tt1375666"])
    assert waits == [5.0, 5.0, 5.0]
