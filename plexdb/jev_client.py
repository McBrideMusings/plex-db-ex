"""Asking Jev whether two keywords mean the same thing, and what roles one
keyword plays.

`synonyms.py` depends on `Judge` and `roles.py` on `RoleJudge`, not on
`LiveJev`, so a test can substitute a fake without a socket — the same split as
`tmdb_client.py`.

Jev's `noul` answer is a probability between 0 and 1 that the statement is true.
The pair request also carries a `score` question; Jev answers both from one
state, and only the `noul` answer is kept (ADR-0018 stores the judge's own
answer, not a conclusion drawn from it). The role request carries one `noul`
question per role in `keywords.ROLES`, all about the same keyword.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .errors import JevError, JevRejected
from .keywords import ROLES

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

_ROLE_CONTEXT = "A tag attached to movies and TV shows in a keyword database."
#: One `noul` question per role, keyed by the role name `keyword_roles.role` stores.
_ROLE_INSTRUCTIONS = {
    "tone": "The tag names a tone or mood a title has, such as bleak, whimsical or tense.",
    "era": "The tag names a time period a title is set in, such as the 1970s or the Victorian era.",
    "region": "The tag names a place a title is set in, such as Los Angeles, Tokyo or Scotland.",
    "theme": "The tag names a theme a title is about, such as grief, revenge or redemption.",
    "character_trait": (
        "The tag names a trait of a title's characters, such as antihero, genius or loner."
    ),
}


def judge_all[I, T](ask: Callable[[I], T], inputs: list[I], workers: int) -> list[T | JevRejected]:
    """`ask(item)` for every item on `workers` threads, results in order. An item
    Jev refuses (`JevRejected`) comes back as that error in its place; any other
    failure cancels the calls not yet started and ends the call with nothing
    returned."""
    if not inputs:
        return []
    with ThreadPoolExecutor(workers) as pool:
        futures = [pool.submit(ask, item) for item in inputs]
        try:
            outcomes: list[T | JevRejected] = []
            for future in futures:
                try:
                    outcomes.append(future.result())
                except JevRejected as err:
                    outcomes.append(err)
            return outcomes
        except BaseException:
            for future in futures:
                future.cancel()
            raise


@dataclass(frozen=True)
class Verdict:
    #: Jev's `noul` answer, 0 to 1, exactly as given.
    score: float
    #: The model that answered, e.g. `jev-1.13.0` — not the `jev-latest` alias asked for.
    model: str


@dataclass(frozen=True)
class RoleVerdict:
    #: Jev's `noul` answer per role in `keywords.ROLES`, 0 to 1, exactly as given.
    scores: dict[str, float]
    #: The model that answered, e.g. `jev-1.13.0`.
    model: str


class Judge(Protocol):
    def judge(self, tag_a: str, tag_b: str) -> Verdict:
        """Whether two readable keyword spellings mean the same thing.

        Safe to call from several threads at once.
        """
        ...


class RoleJudge(Protocol):
    def judge_roles(self, tag: str) -> RoleVerdict:
        """How likely one readable keyword spelling is to play each role.

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

    def judge_roles(self, tag: str) -> RoleVerdict:
        body = {
            "state": {"tag": tag, "context": _ROLE_CONTEXT},
            "model": _MODEL,
            "questions": {
                role: {"type": "noul", "instructions": instructions}
                for role, instructions in _ROLE_INSTRUCTIONS.items()
            },
        }
        answer = self._post(body)
        try:
            scores = {role: float(answer["answers"][role]["noul"]) for role in ROLES}
            model = str(answer["model"])
        except (KeyError, TypeError, ValueError):
            raise JevError(
                "Jev returned an answer without a noul score per role and a model"
            ) from None
        if not model or any(not 0.0 <= score <= 1.0 for score in scores.values()):
            raise JevError(f"Jev returned role scores {scores} from model {model!r}")
        return RoleVerdict(scores=scores, model=model)

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
