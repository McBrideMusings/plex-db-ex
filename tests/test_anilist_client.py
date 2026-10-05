"""`LiveAniListClient` against the recorded responses, through
`httpx.MockTransport` — never the live API."""

from __future__ import annotations

import json

import httpx
import pytest
from anilist_fixtures import load

from plexdb.anilist_client import ENDPOINT, MAPPING_URL, QUERY, LiveAniListClient
from plexdb.errors import AniListError


def _client(handler: httpx.MockTransport, waits: list[float]) -> LiveAniListClient:
    return LiveAniListClient(httpx.Client(transport=handler), sleep=waits.append)


def test_one_request_carries_every_id_and_parses_rank_category_and_spoiler() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=load("page_batch.json"))

    tags = _client(httpx.MockTransport(handler), []).tags([5114, 300, 1225, 999999999])

    assert len(seen) == 1
    assert str(seen[0].url) == ENDPOINT
    body = json.loads(seen[0].content)
    assert body == {
        "query": QUERY,
        "variables": {"ids": [5114, 300, 1225, 999999999], "perPage": 50},
    }
    assert sorted(tags) == [300, 1225, 5114]
    by_name = {t.name: t for t in tags[5114]}
    assert by_name["Alchemy"].rank == 97 and by_name["Alchemy"].category == "Theme-Fantasy"
    assert not by_name["Alchemy"].spoiler
    assert by_name["Conspiracy"].spoiler  # isMediaSpoiler
    assert by_name["Tragedy"].spoiler  # isGeneralSpoiler


def test_more_than_a_page_of_ids_is_split_into_requests() -> None:
    asked: list[list[int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(json.loads(request.content)["variables"]["ids"])
        return httpx.Response(200, json={"data": {"Page": {"media": []}}})

    _client(httpx.MockTransport(handler), []).tags(list(range(1, 121)))

    assert [len(ids) for ids in asked] == [50, 50, 20]


def test_a_429_backs_off_for_the_wait_it_asks_for_and_continues() -> None:
    answers = [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, json=load("page_batch.json")),
    ]
    waits: list[float] = []

    tags = _client(httpx.MockTransport(lambda r: answers.pop(0)), waits).tags([5114])

    assert waits[0] == 7.0
    assert 5114 in tags


def test_exhausted_429s_raise() -> None:
    waits: list[float] = []
    client = _client(
        httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "5"})), waits
    )

    with pytest.raises(AniListError, match="kept answering 429"):
        client.tags([5114])
    assert waits == [5.0, 5.0, 5.0]


def test_a_retry_after_past_the_cap_fails_without_waiting() -> None:
    waits: list[float] = []
    client = _client(
        httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "600"})), waits
    )

    with pytest.raises(AniListError, match="600 s wait"):
        client.tags([5114])
    assert waits == []


def test_an_unparseable_retry_after_waits_a_minute() -> None:
    answers = [
        httpx.Response(429, headers={"Retry-After": "soon"}),
        httpx.Response(200, json=load("page_batch.json")),
    ]
    waits: list[float] = []

    _client(httpx.MockTransport(lambda r: answers.pop(0)), waits).tags([5114])

    assert waits[0] == 60.0


def test_graphql_errors_raise() -> None:
    client = _client(
        httpx.MockTransport(
            lambda r: httpx.Response(200, json={"errors": [{"message": "Invalid query"}]})
        ),
        [],
    )

    with pytest.raises(AniListError, match="Invalid query"):
        client.tags([5114])


def test_the_mapping_is_fetched_whole() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=load("mapping_excerpt.json"))

    entries = _client(httpx.MockTransport(handler), []).mapping()

    assert seen == [MAPPING_URL]
    assert [e["anilist_id"] for e in entries] == [300, 1225, 5114]


def test_a_mapping_that_is_not_a_list_raises() -> None:
    client = _client(httpx.MockTransport(lambda r: httpx.Response(200, json={"x": 1})), [])

    with pytest.raises(AniListError, match="not a list"):
        client.mapping()
