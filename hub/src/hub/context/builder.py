"""ContextBuilder: transforms the request just before it is routed to a model.

Stage 2 will implement a builder that searches :class:`~hub.memory.MemoryProvider`
and prepends relevant notes as a system message. The builder receives the
route's ``locality_ceiling`` so it can choose what is safe to inject: see
:func:`max_sensitivity_for`.

Note the ordering: the builder runs once per request, *before* the router picks
a model, and the ceiling is the route's ceiling (not the eventual model's). That
is the conservative choice: a cloud-capable route never receives PRIVATE memory,
even if a local fallback ends up answering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from hub.memory.protocol import Sensitivity
from hub.types import Locality, ModelRequest

Source = Literal["api", "web", "cli", "mqtt", "webhook", "voice"]


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    route: str
    route_reason: str
    locality_ceiling: Locality
    source: Source = "api"
    device_id: str | None = None


def max_sensitivity_for(ceiling: Locality) -> Sensitivity:
    """Most sensitive memory that may be injected into a request with this ceiling."""
    return Sensitivity.PRIVATE if ceiling is Locality.LOCAL else Sensitivity.PERSONAL


@runtime_checkable
class ContextBuilder(Protocol):
    async def build(self, req: ModelRequest, ctx: RequestContext) -> ModelRequest: ...


class PassThroughContextBuilder:
    async def build(self, req: ModelRequest, ctx: RequestContext) -> ModelRequest:
        return req
