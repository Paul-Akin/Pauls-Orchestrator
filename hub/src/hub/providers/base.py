"""The provider adapter contract.

One adapter instance per *provider* (not per model). The router asks an adapter
for its locality, its availability, and a stream of chunks for a given
:class:`~hub.config.ModelSpec`. Swapping LiteLLM for a vendor SDK means writing a
new class that satisfies this protocol; nothing else changes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from hub.config import ModelSpec
from hub.locality import ProviderName
from hub.types import Locality, ModelRequest, Usage


@dataclass(frozen=True)
class Availability:
    available: bool
    detail: str


@dataclass(frozen=True)
class ModelChunk:
    """A piece of a streamed answer. The last chunk usually carries ``usage``."""

    text: str = ""
    usage: Usage | None = None


class ProviderErrorKind(StrEnum):
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    BAD_REQUEST = "bad_request"
    OTHER = "other"


class ProviderError(Exception):
    """Normalized provider failure. ``message`` must never contain secrets."""

    def __init__(self, kind: ProviderErrorKind, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


@runtime_checkable
class ProviderAdapter(Protocol):
    @property
    def name(self) -> ProviderName: ...

    def locality_for(self, spec: ModelSpec) -> Locality:
        """Where a call for ``spec`` would execute. Must fail closed (CLOUD)."""
        ...

    async def availability(self) -> Availability: ...

    def stream(self, spec: ModelSpec, req: ModelRequest) -> AsyncIterator[ModelChunk]:
        """Yield chunks. Raise :class:`ProviderError` on failure."""
        ...
