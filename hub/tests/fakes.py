"""Test doubles for providers. No network, deterministic."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Literal

from hub.config import ModelSpec
from hub.locality import ProviderName, model_locality
from hub.providers.base import Availability, ModelChunk, ProviderError, ProviderErrorKind
from hub.providers.registry import ProviderRegistry
from hub.types import Locality, ModelRequest, Usage

Behavior = Literal["ok", "error", "hang", "fail_mid", "crash", "silent"]


@dataclass
class FakeAdapter:
    """Scriptable adapter. ``behavior`` decides what ``stream`` does."""

    provider: ProviderName
    behavior: Behavior = "ok"
    available: bool = True
    error_kind: ProviderErrorKind = ProviderErrorKind.AUTH
    reply: str = "answer from {model}"
    api_base: str | None = "http://127.0.0.1:11434"
    calls: list[str] = field(default_factory=list)

    @property
    def name(self) -> ProviderName:
        return self.provider

    def locality_for(self, spec: ModelSpec) -> Locality:
        # Real locality rules, so tests exercise production logic.
        base = self.api_base if self.provider is ProviderName.OLLAMA else None
        return model_locality(self.provider, spec.model, base)

    async def availability(self) -> Availability:
        return Availability(self.available, "fake available" if self.available else "fake: no key")

    async def stream(self, spec: ModelSpec, req: ModelRequest) -> AsyncIterator[ModelChunk]:
        self.calls.append(spec.alias)
        if self.behavior == "error":
            raise ProviderError(self.error_kind, "fake failure")
        if self.behavior == "crash":
            raise RuntimeError("adapter bug")
        if self.behavior == "hang":
            await asyncio.sleep(3600)
        if self.behavior == "silent":
            yield ModelChunk(usage=Usage(input_tokens=1, output_tokens=0))
            return
        words = self.reply.format(model=spec.model).split(" ")
        for i, word in enumerate(words):
            if self.behavior == "fail_mid" and i == 1:
                raise ProviderError(ProviderErrorKind.UNAVAILABLE, "connection dropped")
            yield ModelChunk(text=word if i == 0 else " " + word)
        yield ModelChunk(usage=Usage(input_tokens=10, output_tokens=len(words)))


class CloudTripwire:
    """A cloud adapter that fails the test if the router so much as asks it anything
    beyond its locality.

    ``allow_status=True`` permits ``availability()`` (the app's startup/status
    probe, which for real cloud adapters is a local key check with no network);
    ``stream()`` always trips.
    """

    def __init__(self, provider: ProviderName, *, allow_status: bool = False) -> None:
        self.provider = provider
        self.allow_status = allow_status
        self.touched = 0

    @property
    def name(self) -> ProviderName:
        return self.provider

    def locality_for(self, spec: ModelSpec) -> Locality:
        return model_locality(self.provider, spec.model, None)

    async def availability(self) -> Availability:
        if self.allow_status:
            return Availability(True, "tripwire (status allowed)")
        self.touched += 1
        raise AssertionError(f"{self.provider} availability() called on a local-only path")

    def stream(self, spec: ModelSpec, req: ModelRequest) -> AsyncIterator[ModelChunk]:
        self.touched += 1
        raise AssertionError(f"{self.provider} stream() called on a local-only path")


def registry(adapters: Mapping[ProviderName, object]) -> ProviderRegistry:
    full: dict[ProviderName, object] = {p: FakeAdapter(p) for p in ProviderName}
    full.update(adapters)
    return ProviderRegistry(full)  # type: ignore[arg-type]


def chat_service(
    routes_dict: dict[str, object],
    adapters: Mapping[ProviderName, object] | None = None,
    *,
    usage_store: object | None = None,
    context_builder: object | None = None,
) -> object:
    """Build a ChatService wired to fakes. Returns ``ChatService`` (typed loosely for tests)."""
    from hub.config import parse_config
    from hub.context.builder import PassThroughContextBuilder
    from hub.pipeline import ChatService
    from hub.routing.classifier import KeywordClassifier
    from hub.routing.router import Router

    routes = parse_config({}, routes_dict).routes
    return ChatService(
        routes=routes,
        router=Router(routes, registry(adapters or {})),
        classifier=KeywordClassifier(routes),
        context_builder=context_builder or PassThroughContextBuilder(),  # type: ignore[arg-type]
        usage_store=usage_store,  # type: ignore[arg-type]
    )
