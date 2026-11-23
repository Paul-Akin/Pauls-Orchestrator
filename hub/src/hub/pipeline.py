"""The request pipeline every interface goes through.

    ChatRequest
      -> resolve route (explicit, or Classifier for "auto")
      -> ContextBuilder.build()        [stage 2: memory injection]
      -> Router.stream()               [fallback + locality guard]
      -> cost + UsageStore.record() + structured log
      -> StreamEvent* (route, attempt, delta..., done | error)

REST, SSE, CLI (via REST), and later MQTT/voice all call :class:`ChatService`, so
behaviour (logging, cost, privacy) cannot differ between interfaces.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Callable

import structlog

from hub.config import RoutesConfig
from hub.context.builder import ContextBuilder, RequestContext, Source
from hub.routing.classifier import Classification, Classifier
from hub.routing.router import (
    Router,
    RouterAttempt,
    RouterCompleted,
    RouterDelta,
    RouterFailed,
    UnknownRouteError,
)
from hub.types import (
    AttemptEvent,
    ChatMetadata,
    ChatRequest,
    ChatResponse,
    DeltaEvent,
    DoneEvent,
    ErrorEvent,
    ModelRequest,
    RouteChosenEvent,
    StreamEvent,
)
from hub.usage.pricing import compute_cost
from hub.usage.store import UsageRecord, UsageStore

log = structlog.get_logger(__name__)


class ChatFailedError(Exception):
    def __init__(self, event: ErrorEvent) -> None:
        super().__init__(event.message)
        self.event = event


class ChatService:
    def __init__(
        self,
        *,
        routes: RoutesConfig,
        router: Router,
        classifier: Classifier,
        context_builder: ContextBuilder,
        usage_store: UsageStore | None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._routes = routes
        self._router = router
        self._classifier = classifier
        self._context_builder = context_builder
        self._usage = usage_store
        self._clock = clock

    def resolve_route(self, req: ChatRequest) -> Classification:
        """Pick the route. Raises :class:`UnknownRouteError` *before* any streaming starts."""
        if req.route == "auto":
            return self._classifier.classify(req)
        self._router.resolve(req.route)  # raises UnknownRouteError
        return Classification(req.route, "explicit")

    async def stream(
        self,
        req: ChatRequest,
        *,
        source: Source = "api",
        device_id: str | None = None,
        classification: Classification | None = None,
    ) -> AsyncIterator[StreamEvent]:
        started = self._clock()
        chosen = classification or self.resolve_route(req)
        route = self._router.resolve(chosen.route)
        ctx = RequestContext(
            request_id=uuid.uuid4().hex[:12],
            route=chosen.route,
            route_reason=chosen.reason,
            locality_ceiling=route.locality_ceiling,
            source=source,
            device_id=device_id,
        )
        log_ctx = log.bind(request_id=ctx.request_id, route=ctx.route, source=source)
        yield RouteChosenEvent(request_id=ctx.request_id, route=ctx.route, reason=ctx.route_reason)

        model_req = await self._context_builder.build(
            ModelRequest(
                messages=req.messages, max_tokens=req.max_tokens, temperature=req.temperature
            ),
            ctx,
        )

        async for event in self._router.stream(ctx.route, model_req, ctx.locality_ceiling):
            match event:
                case RouterAttempt(spec=spec):
                    yield AttemptEvent(
                        model_alias=spec.alias, provider=spec.provider.value, model=spec.model
                    )
                case RouterDelta(text=text):
                    yield DeltaEvent(text=text)
                case RouterCompleted():
                    meta = self._metadata(ctx, event, started)
                    log_ctx.info(
                        "chat_completed",
                        model_alias=meta.model_alias,
                        provider=meta.provider,
                        model=meta.model,
                        locality=meta.locality.value,
                        fell_back=meta.fell_back,
                        input_tokens=meta.usage.input_tokens if meta.usage else None,
                        output_tokens=meta.usage.output_tokens if meta.usage else None,
                        usage_estimated=meta.usage.estimated if meta.usage else None,
                        cost_usd=meta.cost.usd,
                        cost_status=meta.cost.status.value,
                        latency_ms=meta.latency_ms,
                    )
                    await self._record_ok(meta, source)
                    yield DoneEvent(metadata=meta)
                case RouterFailed():
                    latency = int((self._clock() - started) * 1000)
                    log_ctx.error(
                        "chat_failed",
                        reason=event.message,
                        partial=event.partial,
                        attempts=[f"{a.model_alias}:{a.outcome.value}" for a in event.attempts],
                        latency_ms=latency,
                    )
                    await self._record_failed(ctx, event, latency)
                    yield ErrorEvent(
                        request_id=ctx.request_id, message=event.message, attempts=event.attempts
                    )

    async def complete(
        self, req: ChatRequest, *, source: Source = "api", device_id: str | None = None
    ) -> ChatResponse:
        """Non-streaming convenience wrapper. Raises :class:`ChatFailedError`."""
        parts: list[str] = []
        async for event in self.stream(req, source=source, device_id=device_id):
            if isinstance(event, DeltaEvent):
                parts.append(event.text)
            elif isinstance(event, DoneEvent):
                return ChatResponse(content="".join(parts), metadata=event.metadata)
            elif isinstance(event, ErrorEvent):
                raise ChatFailedError(event)
        raise RuntimeError("stream ended without a terminal event")  # pragma: no cover

    def _metadata(self, ctx: RequestContext, done: RouterCompleted, started: float) -> ChatMetadata:
        spec = done.spec
        return ChatMetadata(
            request_id=ctx.request_id,
            route=ctx.route,
            route_reason=ctx.route_reason,
            model_alias=spec.alias,
            provider=spec.provider.value,
            model=spec.model,
            locality=done.locality,
            usage=done.usage,
            cost=compute_cost(spec.price, done.usage),
            fell_back=len(done.attempts) > 1,
            attempts=done.attempts,
            latency_ms=int((self._clock() - started) * 1000),
        )

    async def _record_ok(self, meta: ChatMetadata, source: str) -> None:
        if self._usage is None:
            return
        await self._safe_record(
            UsageRecord(
                request_id=meta.request_id,
                source=source,
                route=meta.route,
                route_reason=meta.route_reason,
                outcome="ok",
                model_alias=meta.model_alias,
                provider=meta.provider,
                model=meta.model,
                locality=meta.locality.value,
                input_tokens=meta.usage.input_tokens if meta.usage else None,
                output_tokens=meta.usage.output_tokens if meta.usage else None,
                usage_estimated=meta.usage.estimated if meta.usage else None,
                cost_usd=meta.cost.usd,
                cost_status=meta.cost.status.value,
                fell_back=meta.fell_back,
                latency_ms=meta.latency_ms,
                attempts=[a.model_dump(mode="json") for a in meta.attempts],
            )
        )

    async def _record_failed(self, ctx: RequestContext, failed: RouterFailed, latency: int) -> None:
        if self._usage is None:
            return
        await self._safe_record(
            UsageRecord(
                request_id=ctx.request_id,
                source=ctx.source,
                route=ctx.route,
                route_reason=ctx.route_reason,
                outcome="failed",
                model_alias=None,
                provider=None,
                model=None,
                locality=None,
                input_tokens=None,
                output_tokens=None,
                usage_estimated=None,
                cost_usd=None,
                cost_status=None,
                fell_back=len(failed.attempts) > 1,
                latency_ms=latency,
                attempts=[a.model_dump(mode="json") for a in failed.attempts],
            )
        )

    async def _safe_record(self, record: UsageRecord) -> None:
        # Accounting must never break a chat that already succeeded.
        assert self._usage is not None
        try:
            await self._usage.record(record)
        except Exception as exc:
            log.error("usage_record_failed", request_id=record.request_id, error=type(exc).__name__)


__all__ = ["ChatFailedError", "ChatService", "UnknownRouteError"]
