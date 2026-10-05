"""`LiveMDBListClient` against the recorded responses in `fixtures/mdblist/`.

Driven through `httpx.MockTransport`, so the real request-building, paging and
error handling run without a socket. The recordings are trimmed copies of what
the live service returned on 2026-08-11 — the response *shape* is the thing
under test, because every bug this module exists to prevent is a
misreading of that shape.
"""

from __future__ import annotations

import json

import httpx
import pytest
from mdblist_fixtures import load

from plexdb.errors import MDBListError, MDBListQuotaError
from plexdb.mdblist_client import LiveMDBListClient

KEY = "test-key"


def _client(handler: object) -> LiveMDBListClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return LiveMDBListClient(
        KEY, http=httpx.Client(transport=transport, headers={"User-Agent": "plexdb"})
    )


def test_top_lists_carries_size_and_likes_verbatim() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/lists/top"
        assert request.url.params["apikey"] == KEY
        return httpx.Response(200, json=load("lists_top.json"))

    lists = _client(handler).top_lists()

    assert [(entry.list_id, entry.size, entry.likes) for entry in lists] == [
        (2194, 300, 4509),
        (14, 48, 961),
        (9001, 0, 0),
    ]


def test_a_list_with_zero_likes_is_zero_not_absent() -> None:
    """Nullability carries meaning (ADR-0012).

    A brand-new list genuinely has no followers yet, and that is a different
    statement from "MDBList publishes no follower count for this". Collapsing
    the first onto `None` would throw away a real measurement.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load("lists_top.json"))

    newcomer = next(entry for entry in _client(handler).top_lists() if entry.list_id == 9001)

    assert newcomer.likes == 0
    assert newcomer.size == 0


def test_a_missing_number_stays_none_rather_than_becoming_zero() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": 5, "name": "No Numbers", "slug": "n"}])

    entry = _client(handler).top_lists()[0]

    assert entry.size is None
    assert entry.likes is None


def test_entries_read_both_arrays_not_the_objects_keys() -> None:
    """The response is an object, not an array.

    Iterating it walks the keys — the strings "movies", "shows", "pagination"
    — which is what the client being absorbed did. Both arrays are read here.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load("list_2194_items.json"))

    entries = _client(handler).list_entries(2194)

    assert [entry.media_type for entry in entries] == ["show", "show"]
    assert dict(entries[1].external_ids) == {
        "imdb": "tt0903747",
        "tmdb": "1396",
        "tvdb": "81189",
    }


def test_a_list_is_paged_until_has_more_clears() -> None:
    pages = {0: "list_14_items_page1.json", 1000: "list_14_items_page2.json"}
    asked: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        asked.append(offset)
        return httpx.Response(200, json=load(pages[offset]))

    entries = _client(handler).list_entries(14)

    assert asked == [0, 1000]
    assert [dict(entry.external_ids)["imdb"] for entry in entries] == [
        "tt0468569",
        "tt0372784",
    ]


def test_a_403_says_the_agent_might_be_the_cause_not_only_the_key() -> None:
    """An agentless request gets 403 with a perfectly good key.

    Reported verbatim by the live service as `error code: 1010`. A message
    naming only the credential sends someone off to regenerate a key that was
    never the problem.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="error code: 1010")

    with pytest.raises(MDBListError) as err:
        _client(handler).top_lists()

    assert "User-Agent" in str(err.value)
    assert "MDBLIST_API_KEY" in str(err.value)


def test_an_error_never_prints_the_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(MDBListError) as err:
        _client(handler).top_lists()

    assert KEY not in str(err.value)


def test_a_response_of_the_wrong_shape_is_an_error_not_a_silent_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"lists": []})

    with pytest.raises(MDBListError):
        _client(handler).top_lists()


def test_a_200_with_a_non_json_body_raises_mdblist_error_not_a_json_traceback() -> None:
    # A WAF interstitial or captive portal can answer 200 with an HTML page
    # instead of the JSON MDBList normally returns — issue #49.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<!DOCTYPE html><title>Just a moment...</title>",
            headers={"content-type": "text/html"},
        )

    with pytest.raises(MDBListError, match="not JSON"):
        _client(handler).top_lists()


def test_a_ratings_batch_posts_the_ids_and_reads_each_site_verbatim() -> None:
    """Against the live recording of 2026-10-05: four ids asked, three known."""
    asked = ["tt21301418", "tt0099180", "tt0089603", "tt9999999999"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/imdb/movie/"
        assert request.url.params["apikey"] == KEY
        assert json.loads(request.content) == {"ids": asked}
        return httpx.Response(200, json=load("batch_imdb_movie.json"))

    found = {t.imdb_id: t for t in _client(handler).ratings("movie", asked)}

    assert sorted(found) == ["tt0089603", "tt0099180", "tt21301418"]
    mishima = {r.site: (r.value, r.votes) for r in found["tt0089603"].ratings}
    assert mishima["imdb"] == (7.9, 17303)
    assert mishima["metacriticuser"] == (8.0, 33)
    assert mishima["rogerebert"] == (4.0, None)  # MDBList's own score is null here
    assert mishima["myanimelist"] == (None, None)


def test_a_ratings_batch_answering_an_object_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "nope"})

    with pytest.raises(MDBListError, match="expected a list of titles"):
        _client(handler).ratings("movie", ["tt0089603"])


def test_a_ratings_batch_refuses_more_ids_than_one_request_carries() -> None:
    with pytest.raises(ValueError, match="at most 200"):
        _client(lambda request: httpx.Response(200, json=[])).ratings("movie", ["tt1"] * 201)


def test_a_429_is_a_quota_error_naming_the_quota() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"x-ratelimit-remaining": "0"})

    with pytest.raises(MDBListQuotaError, match="quota is spent") as err:
        _client(handler).ratings("movie", ["tt0089603"])

    assert KEY not in str(err.value)


def test_an_answer_whose_ids_is_not_an_object_is_skipped_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"ids": ["tt0089603"], "ratings": []}])

    assert _client(handler).ratings("movie", ["tt0089603"]) == []
