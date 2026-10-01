"""Asking Jev whether two keywords mean the same thing.

`synonyms.py` depends on `Judge`, not on `LiveJev`, so a test can substitute a
fake without a socket — the same split as `tmdb_client.py`.

Jev's `noul` answer is a probability between 0 and 1 that the statement is true.
The same request also carries a `score` question; Jev answers both from one
state, and only the `noul` answer is kept (ADR-0018 stores the judge's own
answer, not a conclusion drawn from it).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .errors import JevError, JevRejected

_URL = "https://api.typesafe.ai/v1/systemone"
#: Statuses that mean this request is invalid. A bad key (401, 403) or an outage
#: (5xx) would refuse every pair, so those stay plain `JevError`s that stop the run.
_REJECTED_STATUSES = frozenset({400, 422})
_MODEL = "jev-latest"
_DEFAULT_TIMEOUT = 120.0
_ATTEMPTS = 8
_BACKOFF_SECONDS = 0.5

_SCORE_INSTRUCTIONS = (
    "Two tags from a movie and TV keyword database. Would a viewer who searches or likes one "
    "tag be equally well served by the other, so that merging them into one tag loses nothing?"
)
_SCORE_CRITERIA = [
    "different tags: related at most, merging would lose information",
    "overlapping but not interchangeable, a curator should decide",
    "same meaning: interchangeable synonyms, safe to merge",
]
_NOUL_INSTRUCTIONS = "Do the two tags mean the same thing?"


@dataclass(frozen=True)
class Verdict:
    #: Jev's `noul` answer, 0 to 1, exactly as given.
    score: float
    #: The model that answered, e.g. `jev-1.13.0` — not the `jev-latest` alias asked for.
    model: str


class Judge(Protocol):
    def judge(self, tag_a: str, tag_b: str) -> Verdict:
        """Whether two readable keyword spellings mean the same thing.

        Safe to call from several threads at once.
        """
        ...


class LiveJev:
    """The one `Judge` that reaches the real service, over `httpx`."""

    def __init__(
        self,
        api_key: str,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._key = api_key
        self._http = http or httpx.Client(timeout=_DEFAULT_TIMEOUT)
        self._sleep = sleep

    def judge(self, tag_a: str, tag_b: str) -> Verdict:
        body = {
            "state": {"tag_a": tag_a, "tag_b": tag_b},
            "model": _MODEL,
            "questions": {
                "s": {
                    "type": "score",
                    "instructions": _SCORE_INSTRUCTIONS,
                    "criteria": _SCORE_CRITERIA,
                },
                "n": {"type": "noul", "instructions": _NOUL_INSTRUCTIONS},
            },
        }
        answer = self._post(body)
        try:
            score = float(answer["answers"]["n"]["noul"])
            model = str(answer["model"])
        except (KeyError, TypeError, ValueError):
            raise JevError("Jev returned an answer without a noul score and a model") from None
        if not 0.0 <= score <= 1.0 or not model:
            raise JevError(f"Jev returned a noul score of {score} from model {model!r}")
        return Verdict(score=score, model=model)

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST with backoff on 429; 400 and 422 raise `JevRejected`. Error messages
        carry the status code and the exception's type only, never the request or
        the key."""
        for attempt in range(_ATTEMPTS):
            try:
                resp = self._http.post(
                    _URL, json=body, headers={"Authorization": f"Bearer {self._key}"}
                )
            except httpx.HTTPError as err:
                raise JevError(f"cannot reach Jev at {_URL}: {type(err).__name__}") from None
            if resp.status_code == 429:
                self._sleep(_BACKOFF_SECONDS * 2**attempt)
                continue
            if resp.status_code in _REJECTED_STATUSES:
                raise JevRejected(f"Jev returned {resp.status_code} for {_URL}")
            if resp.status_code != 200:
                raise JevError(f"Jev returned {resp.status_code} for {_URL}")
            try:
                result: dict[str, Any] = resp.json()
            except ValueError:
                raise JevError(f"Jev returned a response that is not JSON for {_URL}") from None
            return result
        raise JevError(f"Jev answered 429 {_ATTEMPTS} times in a row for {_URL}")
