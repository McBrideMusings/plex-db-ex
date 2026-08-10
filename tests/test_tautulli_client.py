"""`LiveTautulliClient` against recorded responses, never a live server.

`tests/fixtures/tautulli/history.json` mirrors the shape a real `get_history`
call returns (verified against a live server, 2026-08-10), with synthetic
ids, names, and IP addresses in place of a real account's data — watch
history is personal in a way a movie's metadata is not, and Tautulli history
rows carry an IP address, which is exactly the kind of value that must never
land in a committed fixture. `httpx.MockTransport` serves the fixture back
over the exact request-building and JSON-parsing code `LiveTautulliClient`
uses against a real server, so this is a genuine test of the adapter, just
with no socket.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from plexdb import tautulli_client
from plexdb.errors import TautulliError
from plexdb.tautulli_client import LiveTautulliClient

FIXTURES = Path(__file__).parent / "fixtures" / "tautulli"


def _load(name: str) -> dict[str, object]:
    data: dict[str, object] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


def _client(http: httpx.Client) -> LiveTautulliClient:
    return LiveTautulliClient("http://tautulli.example:8181", "the-test-key", http=http)


def test_history_returns_every_row_the_server_reports_including_in_progress() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["apikey"] == "the-test-key"
        assert request.url.params["cmd"] == "get_history"
        return httpx.Response(200, json=_load("history.json"))

    http = httpx.Client(transport=httpx.MockTransport(_handler))
    rows = _client(http).history()

    assert [r["id"] for r in rows] == [None, None, 35888, 35889]
    assert rows[0]["rating_key"] == 50822
    assert rows[3]["duration"] == 1039


def test_history_reads_the_fields_the_matching_logic_needs() -> None:
    http = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=_load("history.json")))
    )
    rows = _client(http).history()

    completed = next(r for r in rows if r["id"] == 35889)
    assert completed["user_id"] == 501
    assert completed["machine_id"] == "device-alpha-001"
    assert completed["stopped"] == 1786384547
    assert completed["paused_counter"] == 0
    assert completed["percent_complete"] == 100
    assert completed["ip_address"] == "203.0.113.10"


def test_history_paginates_when_a_page_comes_back_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A small page size makes a 3-row history span two pages without a
    # thousand-row fixture.
    monkeypatch.setattr(tautulli_client, "_HISTORY_PAGE_SIZE", 2)
    pages = {
        0: [
            {"id": 1, "rating_key": 1, "stopped": 100},
            {"id": 2, "rating_key": 2, "stopped": 101},
        ],
        2: [
            {"id": 3, "rating_key": 3, "stopped": 102},
        ],
    }

    def _paged(request: httpx.Request) -> httpx.Response:
        assert request.url.params["length"] == "2"
        start = int(request.url.params["start"])
        return httpx.Response(
            200,
            json={
                "response": {
                    "result": "success",
                    "data": {"recordsFiltered": 3, "recordsTotal": 3, "data": pages[start]},
                }
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(_paged))
    rows = _client(http).history()

    assert [r["id"] for r in rows] == [1, 2, 3]


def test_a_page_shorter_than_requested_stops_pagination_without_a_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tautulli_client, "_HISTORY_PAGE_SIZE", 5)
    calls = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "response": {
                    "result": "success",
                    "data": {
                        "recordsFiltered": 2,
                        "recordsTotal": 2,
                        "data": [
                            {"id": 1, "rating_key": 1, "stopped": 100},
                            {"id": 2, "rating_key": 2, "stopped": 101},
                        ],
                    },
                }
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(_handler))
    rows = _client(http).history()

    assert len(rows) == 2
    assert calls == 1


def test_users_returns_the_bare_list_get_users_reports_not_a_datatables_wrapper() -> None:
    """Confirmed live (2026-08-10): unlike `get_history`, `get_users`'s
    `response.data` is the user list itself, with no `{"data": [...]}`
    wrapper. Synthetic ids/names, same reasoning as `history.json` — a user
    list is personal data."""

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["apikey"] == "the-test-key"
        assert request.url.params["cmd"] == "get_users"
        return httpx.Response(
            200,
            json={
                "response": {
                    "result": "success",
                    "data": [
                        {"user_id": 0, "username": "Local", "is_admin": 0},
                        {"user_id": 987654321, "username": "server-owner", "is_admin": 1},
                        {"user_id": 501, "username": "shared-user", "is_admin": 0},
                    ],
                }
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(_handler))
    users = _client(http).users()

    assert [(u["user_id"], u["username"]) for u in users] == [
        (0, "Local"),
        (987654321, "server-owner"),
        (501, "shared-user"),
    ]


def test_users_returns_an_empty_list_if_the_response_shape_is_unexpected() -> None:
    """`_call` falls back to `{}` for a missing `data` key (the same default
    `history()` relies on for its own unwrap); `users()` must not raise on a
    non-list `data`, just report nothing."""
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"response": {"result": "success"}})
        )
    )

    assert _client(http).users() == []


def test_an_unreachable_server_raises_tautulli_error_not_a_raw_httpx_error() -> None:
    def _broken(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    http = httpx.Client(transport=httpx.MockTransport(_broken))

    with pytest.raises(TautulliError, match="cannot reach Tautulli"):
        _client(http).history()


def test_a_non_200_response_raises_tautulli_error() -> None:
    def _unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"response": {"result": "error"}})

    http = httpx.Client(transport=httpx.MockTransport(_unauthorized))

    with pytest.raises(TautulliError, match="Tautulli returned 401"):
        _client(http).history()


def test_a_response_reporting_failure_raises_tautulli_error() -> None:
    def _failure(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"response": {"result": "error", "message": "Invalid apikey"}},
        )

    http = httpx.Client(transport=httpx.MockTransport(_failure))

    with pytest.raises(TautulliError, match="Invalid apikey"):
        _client(http).history()


def test_an_error_response_never_leaks_the_api_key_into_the_raised_message() -> None:
    # Tautulli authenticates by query parameter, so the key rides in the
    # request URL — and httpx's own `raise_for_status()` formats its message
    # as "... for url '<response.url>'", which carries the full query string.
    # Same leak `tmdb_client`'s equivalent test pins.
    def _unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"response": {"result": "error"}})

    http = httpx.Client(transport=httpx.MockTransport(_unauthorized))
    client = LiveTautulliClient("http://tautulli.example:8181", "the-real-secret-key", http=http)

    with pytest.raises(TautulliError) as exc_info:
        client.history()

    assert "the-real-secret-key" not in str(exc_info.value)


def test_the_api_key_never_reaches_a_traceback() -> None:
    import traceback

    key = "SECRET-TAUTULLI-KEY-DO-NOT-LEAK"
    http = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(401, json={})))
    client = LiveTautulliClient("http://tautulli.example:8181", key, http=http)

    try:
        client.history()
    except TautulliError:
        rendered = traceback.format_exc()
    else:  # pragma: no cover - the 401 above always raises
        raise AssertionError("expected TautulliError")

    assert key not in rendered
    assert "apikey=" not in rendered
