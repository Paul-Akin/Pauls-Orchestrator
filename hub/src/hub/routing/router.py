"""Fallback router.

For a route ``primary -> fallback[0] -> fallback[1] ...`` the router tries each
model in order and streams the first one that produces text.

Rules (see README "Routing" and docs/adr/0003):

* **Locality guard.** Before *every* call the adapter's runtime locality is
  compared to the route's ceiling. A ``local_only`` route can never call a
  cloud model, even if config validation was somehow bypassed.
* **Skip, don't fail,** models that are unavailable (no key, daemon down) or
  lack vision when the request carries images.
* **Fallback only before the first text chunk.** After text has been streamed to
  the client, switching models would splice two answers together, so a
  mid-stream failure is reported as an error instead.
* **Timeouts.** ``timeout_s`` bounds time-to-first-chunk and each idle gap
  between chunks. ``deadline_s`` bounds the whole fallback search, so the worst
  case wait is ``deadline_s`` rather than ``sum(timeout_s)``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import structlog

from hub.config import ModelSpec, RoutesConfig, RouteSpec
from hub.providers.base import ProviderError, ProviderErrorKind
from hub.providers.registry import ProviderRegistry
from hub.types import (
    AttemptOutcome,
    AttemptRecord,
    Locality,
    ModelRequest,
    Usage,
    locality_permitted,
)

log = structlog.get_logger(__name__)


class UnknownRouteError(KeyError):
    pass


@dataclass(frozen=True)
class RouterAttempt:
    spec: ModelSpec


@dataclass(frozen=True)
class RouterDelta:
    text: str


@dataclass(frozen=True)
class RouterCompleted:
    spec: ModelSpec
    locality: Locality
    usage: Usage | None
    attempts: list[AttemptRecord] = field(default_factory=list)


@dataclass(frozen=True)
class RouterFailed:
    message: str
    attempts: list[AttemptRecord]
    partial: bool
    """True if some text had already been streamed before the failure."""


RouterEvent = RouterAttempt | RouterDelta | RouterCompleted | RouterFailed


def _record(
    spec: ModelSpec,
    outcome: AttemptOutcome,
    reason: str | None = None,
    latency_ms: int | None = None,
) -> AttemptRecord:
    return AttemptRecord(
        model_alias=spec.alias,
        provider=spec.provider.value,
        model=spec.model,
        outcome=outcome,
        reason=reason,
        latency_ms=latency_ms,
    )


class Router:
    def __init__(
        self,
        routes: RoutesConfig,
        registry: ProviderRegistry,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._routes = routes
        self._registry = registry
        self._clock = clock

    def resolve(self, name: str) -> RouteSpec:
        try:
            return self._routes.routes[name]
        except KeyError:
            raise UnknownRouteError(name) from None

    async def stream(
        self, route_name: str, req: ModelRequest, ceiling: Locality = Locality.CLOUD
    ) -> AsyncIterator[RouterEvent]:
        route = self.resolve(route_name)
        # The strictest of (caller's ceiling, route's ceiling) wins.
        if route.local_only:
            ceiling = Locality.LOCAL
        deadline = self._clock() + route.deadline_s
        attempts: list[AttemptRecord] = []

        for alias in route.chain:
            spec = self._routes.models[alias]
            adapter = self._registry.get(spec.provider)

            locality = adapter.locality_for(spec)
            if not locality_permitted(ceiling, locality):
                log.error(
                    "locality_violation_blocked",
                    route=route_name,
                    model_alias=alias,
                    locality=locality.value,
                    ceiling=ceiling.value,
                )
                attempts.append(
                    _record(spec, AttemptOutcome.SKIPPED, "blocked: not local on local-only route")
                )
                continue
            if req.has_images and not spec.vision:
                attempts.append(_record(spec, AttemptOutcome.SKIPPED, "model lacks vision"))
                continue
            availability = await adapter.availability()
            if not availability.available:
                attempts.append(_record(spec, AttemptOutcome.SKIPPED, availability.detail))
                continue
            if self._clock() >= deadline:
                attempts.append(_record(spec, AttemptOutcome.SKIPPED, "route deadline exceeded"))
                continue

            yield RouterAttempt(spec)
            started = self._clock()
            committed = False
            usage: Usage | None = None
            iterator = adapter.stream(spec, req).__aiter__()
            try:
                while True:
                    limit = spec.timeout_s
                    if not committed:
                        limit = min(limit, max(deadline - self._clock(), 0.0))
                    try:
                        chunk = await asyncio.wait_for(anext(iterator), timeout=limit)
                    except StopAsyncIteration:
                        break
                    if chunk.usage is not None:
                        usage = chunk.usage
                    if chunk.text:
                        committed = True
                        yield RouterDelta(chunk.text)
            except Exception as exc:  # ProviderError, TimeoutError, or adapter bug
                outcome, reason = self._describe_failure(exc)
                latency = int((self._clock() - started) * 1000)
                attempts.append(_record(spec, outcome, reason, latency))
                if committed:
                    log.error(
                        "provider_failed_mid_stream",
                        route=route_name,
                        model_alias=alias,
                        reason=reason,
                    )
                    yield RouterFailed(
                        f"{alias} failed mid-answer: {reason}", attempts, partial=True
                    )
                    return
                log.warning(
                    "provider_failed_falling_back",
                    route=route_name,
                    model_alias=alias,
                    outcome=outcome.value,
                    reason=reason,
                )
                continue
            finally:
                aclose = getattr(iterator, "aclose", None)
                if aclose is not None:
                    await aclose()

            latency = int((self._clock() - started) * 1000)
            attempts.append(_record(spec, AttemptOutcome.OK, None, latency))
            yield RouterCompleted(spec, locality, usage, attempts)
            return

        log.error("route_exhausted", route=route_name, attempts=len(attempts))
        yield RouterFailed(
            f"no model on route {route_name!r} could answer", attempts, partial=False
        )

    @staticmethod
    def _describe_failure(exc: BaseException) -> tuple[AttemptOutcome, str]:
        if isinstance(exc, TimeoutError):
            return AttemptOutcome.TIMEOUT, "timed out"
        if isinstance(exc, ProviderError):
            outcome = (
                AttemptOutcome.TIMEOUT
                if exc.kind is ProviderErrorKind.TIMEOUT
                else AttemptOutcome.ERROR
            )
            return outcome, f"{exc.kind.value}: {exc.message}"
        # An adapter bug must not take the hub down; treat it like a provider error.
        return AttemptOutcome.ERROR, f"unexpected {type(exc).__name__}"
