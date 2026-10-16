"""The Frame encoder client: frames and phrases to CLIP vectors (ADR-0025).

Every call is a `POST {PLEXDB_FRAME_ENCODER_URL}/v1/embeddings` naming
`PLEXDB_FRAME_MODEL`. This client loads no model and decides nothing about when
work runs; whatever serves the URL does. A slow answer is waited out — a broker
holds a request through a cold start — and a refused one is `EncoderUnavailable`.

OpenAI's API defines text input only. The image shape below is the one a real
Infinity server serving a CLIP checkpoint accepted (`plexdb frames
probe-encoder`, recorded in docs/schema.md): each image a `data:image/jpeg;base64,`
URI in `input`, with `"modality": "image"`.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
import numpy as np

from .config import Config
from .errors import ConfigError, EncoderError, EncoderUnavailable

__all__ = [
    "PROBE_IMAGE",
    "PROBE_PHRASE",
    "TIMEOUT_S",
    "Embedded",
    "EncoderClient",
    "configured_reachable",
    "from_config",
]

#: Long enough to wait out a broker loading the model onto a GPU.
TIMEOUT_S = 300.0
#: `reachable` asks only whether the server answers; listing models loads none.
REACHABLE_TIMEOUT_S = 10.0

#: The image `plexdb frames probe-encoder` embeds: a 224 px red square, the size
#: the harvester sends.
PROBE_IMAGE = Path(__file__).with_name("probe.jpg")
PROBE_PHRASE = "a red square"

Modality = Literal["image", "text"]


@dataclass(frozen=True)
class Embedded:
    """One request's answer: one unit-length vector per input, in input order,
    the model the server says produced them, and how long the request took to
    its first byte and to its last."""

    vectors: list[list[float]]
    model: str
    first_byte_s: float
    total_s: float


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


class EncoderClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        http: httpx.Client | None = None,
        log: Callable[[str], None] = _stderr,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._http = http or httpx.Client(timeout=TIMEOUT_S)
        self._log = log

    def embed_images(self, images: Sequence[bytes]) -> list[list[float]]:
        """One vector per JPEG, in the order given."""
        return self.timed_images(images).vectors

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per phrase, in the order given."""
        return self.timed_texts(texts).vectors

    def timed_images(self, images: Sequence[bytes]) -> Embedded:
        """`embed_images`, with the model the server named and the request's timings."""
        uris = [
            f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode('ascii')}" for jpeg in images
        ]
        return self._post(uris, "image")

    def timed_texts(self, texts: Sequence[str]) -> Embedded:
        """`embed_texts`, with the model the server named and the request's timings."""
        return self._post(list(texts), "text")

    def close(self) -> None:
        self._http.close()

    def _post(self, payload: list[str], modality: Modality) -> Embedded:
        if not payload:
            return Embedded(vectors=[], model=self.model, first_byte_s=0.0, total_s=0.0)
        url = f"{self.base_url}/v1/embeddings"
        body = {
            "model": self.model,
            "input": payload,
            "modality": modality,
            "encoding_format": "float",
        }
        started = time.perf_counter()
        try:
            with self._http.stream("POST", url, json=body) as resp:
                first_byte_s = time.perf_counter() - started
                raw = resp.read()
        except httpx.HTTPError as err:
            self._log(f"encoder: POST {url} {modality} x{len(payload)} -> {type(err).__name__}")
            raise EncoderUnavailable(
                f"encoder unavailable at {url}: {type(err).__name__}"
            ) from None
        total_s = time.perf_counter() - started
        self._log(
            f"encoder: POST {url} {modality} x{len(payload)} -> {resp.status_code}, "
            f"{len(raw)} bytes, first byte {first_byte_s * 1000:.0f} ms, "
            f"total {total_s * 1000:.0f} ms"
        )
        if resp.status_code >= 500:
            raise EncoderUnavailable(
                f"encoder unavailable at {url}: {resp.status_code}: {raw[:200]!r}"
            )
        if resp.status_code != 200:
            raise EncoderError(
                f"the encoder at {url} refused the request: {resp.status_code}: {raw[:200]!r}"
            )
        vectors, model = _parse(raw, url, expected=len(payload))
        return Embedded(vectors=vectors, model=model, first_byte_s=first_byte_s, total_s=total_s)

    def reachable(self) -> bool:
        """Whether the server answers `GET /v1/models` with a 2xx inside
        `REACHABLE_TIMEOUT_S`. It never embeds, so it never waits out a cold start."""
        url = f"{self.base_url}/v1/models"
        try:
            resp = self._http.get(url, timeout=REACHABLE_TIMEOUT_S)
        except httpx.HTTPError as err:
            self._log(f"encoder: GET {url} -> {type(err).__name__}")
            return False
        self._log(f"encoder: GET {url} -> {resp.status_code}")
        return resp.is_success


def _parse(raw: bytes, url: str, *, expected: int) -> tuple[list[list[float]], str]:
    try:
        body: dict[str, Any] = json.loads(raw)
        rows = sorted(body["data"], key=lambda row: row["index"])
        matrix = np.array([row["embedding"] for row in rows], dtype=np.float64)
        model = str(body.get("model", ""))
    except (ValueError, KeyError, TypeError):
        raise EncoderError(f"the encoder at {url} returned an unusable body") from None
    if matrix.ndim != 2 or matrix.shape[0] != expected:
        raise EncoderError(f"asked {url} for {expected} vector(s), it returned {len(rows)}")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if not np.all(np.isfinite(matrix)) or not np.all(norms > 0):
        raise EncoderError(f"the encoder at {url} returned a zero or non-finite vector")
    return (matrix / norms).tolist(), model


def from_config(config: Config) -> EncoderClient:
    """The configured encoder, or a `ConfigError` naming what is unset."""
    if not config.frame_encoder_url:
        raise ConfigError("PLEXDB_FRAME_ENCODER_URL is not set, so there is no Frame encoder")
    if not config.frame_model:
        raise ConfigError("PLEXDB_FRAME_MODEL is not set, so the Frame encoder has no model")
    return EncoderClient(config.frame_encoder_url, config.frame_model)


def configured_reachable(config: Config) -> bool | None:
    """What `plexdb frames status` reports as `encoder_reachable`: `None` when no
    encoder URL is set, else whether it answers. Reachability needs no model."""
    if not config.frame_encoder_url:
        return None
    encoder = EncoderClient(config.frame_encoder_url, config.frame_model)
    try:
        return encoder.reachable()
    finally:
        encoder.close()
