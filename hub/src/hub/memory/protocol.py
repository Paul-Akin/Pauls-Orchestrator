"""The memory contract.

The hub's memory store is the single source of truth: no vendor's built-in
memory is used, and nothing is shared vendor-to-vendor (ADR 0002). Stage 2 will
implement this protocol with markdown files plus a vector index, and expose the
same operations through an MCP server.

``Sensitivity`` exists *now*, before any memory does, because it is what keeps
stage 2 from leaking private notes into cloud prompts: a ContextBuilder serving a
cloud-bound request must search with ``max_sensitivity`` below ``PRIVATE``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Protocol, runtime_checkable


class Sensitivity(IntEnum):
    PUBLIC = 0
    """Fine to send anywhere."""
    PERSONAL = 1
    """About you, but acceptable to send to a cloud model you chose."""
    PRIVATE = 2
    """Must never leave this machine (only injected into local-model prompts)."""


@dataclass(frozen=True)
class MemoryItem:
    id: str
    text: str
    source: str
    created_at: datetime
    sensitivity: Sensitivity
    tags: tuple[str, ...] = ()
    score: float | None = None
    """Similarity score when returned from :meth:`MemoryProvider.search`."""


@dataclass(frozen=True)
class NewMemoryItem:
    text: str
    source: str
    sensitivity: Sensitivity = Sensitivity.PERSONAL
    tags: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class MemoryProvider(Protocol):
    async def search(
        self, query: str, *, k: int = 5, max_sensitivity: Sensitivity = Sensitivity.PERSONAL
    ) -> list[MemoryItem]: ...

    async def get(self, item_id: str) -> MemoryItem | None: ...

    async def put(self, item: NewMemoryItem) -> str:
        """Store an item and return its id."""
        ...
