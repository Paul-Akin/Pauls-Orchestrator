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

    @classmethod
    def parse(cls, value: object) -> Sensitivity | None:
        """'private' / 'PRIVATE' / 2 -> Sensitivity; anything unrecognized -> None."""
        if isinstance(value, Sensitivity):
            return value
        if isinstance(value, str):
            return cls.__members__.get(value.strip().upper())
        if isinstance(value, int) and not isinstance(value, bool):
            try:
                return cls(value)
            except ValueError:
                return None
        return None

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class MemoryItem:
    """A note (from :meth:`MemoryProvider.get`) or one chunk of it (from ``search``)."""

    id: str
    """Note id: its path under the memory dir, POSIX style, without '.md'."""
    text: str
    source: str
    created_at: datetime
    sensitivity: Sensitivity
    tags: tuple[str, ...] = ()
    score: float | None = None
    """Fused retrieval score when returned from :meth:`MemoryProvider.search`."""
    title: str = ""
    heading: str = ""
    """Section heading path of the chunk (search results only), e.g. 'Bolts > Preload'."""


@dataclass(frozen=True)
class NewMemoryItem:
    text: str
    source: str
    sensitivity: Sensitivity = Sensitivity.PERSONAL
    tags: tuple[str, ...] = field(default_factory=tuple)
    title: str | None = None


@runtime_checkable
class MemoryProvider(Protocol):
    async def search(
        self, query: str, *, k: int = 5, max_sensitivity: Sensitivity = Sensitivity.PERSONAL
    ) -> list[MemoryItem]: ...

    async def get(
        self, item_id: str, *, max_sensitivity: Sensitivity = Sensitivity.PERSONAL
    ) -> MemoryItem | None:
        """A whole note, or None if missing OR above ``max_sensitivity`` (indistinguishable)."""
        ...

    async def put(self, item: NewMemoryItem) -> str:
        """Store an item and return its id."""
        ...
