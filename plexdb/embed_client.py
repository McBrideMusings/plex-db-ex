"""Embedding short texts through llama-swap's OpenAI-compatible endpoint.

`synonyms.py` depends on `Embedder`, not on `LlamaSwapEmbedder`, so a test can
substitute a fake that returns vectors without a socket — the same split as
`tmdb_client.py`.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from .errors import EmbeddingError

#: The one model the keyword vectors come from. A different model gives
#: different cosines, so the 0.75 cut in `synonyms.py` belongs to this one.
MODEL = "nomic-embed-text"
_DEFAULT_TIMEOUT = 300.0


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per text, in the order given."""
        ...


class LlamaSwapEmbedder:
    """The one `Embedder` that reaches a real server, over `httpx`."""

    def __init__(self, base_url: str, http: httpx.Client | None = None) -> None:
        self._url = f"{base_url.rstrip('/')}/v1/embeddings"
        self._http = http or httpx.Client(timeout=_DEFAULT_TIMEOUT)

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            resp = self._http.post(self._url, json={"model": MODEL, "input": texts})
        except httpx.HTTPError as err:
            raise EmbeddingError(
                f"cannot reach the embedding server at {self._url}: {type(err).__name__}"
            ) from None
        if resp.status_code != 200:
            raise EmbeddingError(
                f"the embedding server returned {resp.status_code} for {self._url}"
                f": {resp.text[:200]}"
            )
        try:
            body: dict[str, Any] = resp.json()
            data = sorted(body["data"], key=lambda row: row["index"])
            vectors = [[float(x) for x in row["embedding"]] for row in data]
        except (ValueError, KeyError, TypeError):
            raise EmbeddingError(
                f"the embedding server returned an unusable body for {self._url}"
            ) from None
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"asked for {len(texts)} embedding(s), the server returned {len(vectors)}"
            )
        return vectors
