"""Text embeddings, from the LOCAL Ollama only.

Embedding a note sends its full text to the embedding model, so the embedder
is subject to the same locality rule as the private route (docs/adr/0005):
startup refuses a non-local embedder (``config.check_runtime``), and
:meth:`OllamaEmbedder.embed` re-checks before every request.

Calls go straight to Ollama's ``POST /api/embed`` with httpx. That keeps the
memory layer independent of LiteLLM (which stays behind ``providers/``) and is
about 20 lines.
"""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable

import httpx

from hub.locality import ProviderName, model_locality
from hub.types import Locality


class EmbedderError(Exception):
    """Embedding failed. Callers degrade to keyword-only search."""


@runtime_checkable
class Embedder(Protocol):
    @property
    def model(self) -> str: ...

    def locality(self) -> Locality: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OllamaEmbedder:
    def __init__(
        self,
        api_base: str,
        model: str,
        *,
        timeout_s: float = 30.0,
        batch_size: int = 32,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = api_base.rstrip("/")
        self._model = model
        self._timeout = timeout_s
        self._batch = batch_size
        self._transport = transport

    @property
    def model(self) -> str:
        return self._model

    def locality(self) -> Locality:
        return model_locality(ProviderName.OLLAMA, self._model, self._base)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self.locality() is not Locality.LOCAL:
            raise EmbedderError("refusing to embed: embedding endpoint is not local")
        if not texts:
            return []
        out: list[list[float]] = []
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                for i in range(0, len(texts), self._batch):
                    batch = texts[i : i + self._batch]
                    r = await client.post(
                        f"{self._base}/api/embed", json={"model": self._model, "input": batch}
                    )
                    r.raise_for_status()
                    vectors = r.json().get("embeddings")
                    if not isinstance(vectors, list) or len(vectors) != len(batch):
                        raise EmbedderError("unexpected /api/embed response shape")
                    out.extend(_validate(v) for v in vectors)
        except httpx.HTTPError as exc:
            raise EmbedderError(f"Ollama embed failed: {type(exc).__name__}") from exc
        dims = {len(v) for v in out}
        if len(dims) != 1:
            raise EmbedderError(f"inconsistent embedding dimensions {sorted(dims)}")
        return out


def _validate(v: object) -> list[float]:
    if not isinstance(v, list) or not v:
        raise EmbedderError("embedding is not a non-empty list")
    vec = [float(x) for x in v]
    if not all(math.isfinite(x) for x in vec):
        raise EmbedderError("embedding contains NaN/inf")
    return vec
