"""``POST /chat``: JSON or Server-Sent Events.

SSE wire format (one event per :data:`hub.types.StreamEvent`)::

    event: route    data: {"type":"route","request_id":"...","route":"...","reason":"..."}
    event: attempt  data: {"type":"attempt","model_alias":"claude",...}   (one per model tried)
    event: delta    data: {"type":"delta","text":"..."}                   (many)
    event: done     data: {"type":"done","metadata":{...}}                 (terminal, success)
    event: error    data: {"type":"error","message":"...","attempts":[...]} (terminal, failure)

Browsers cannot POST with ``EventSource``, so the web UI reads this stream with
``fetch`` + ``ReadableStream``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from hub.api.auth import require_api_token
from hub.pipeline import ChatFailedError, ChatService
from hub.routing.router import UnknownRouteError
from hub.types import ChatRequest, ChatResponse, StreamEvent

router = APIRouter()


def sse_encode(event: StreamEvent) -> bytes:
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n".encode()


async def require_json(request: Request) -> None:
    """Reject non-JSON bodies. Blocks cross-site 'simple' (text/plain) POSTs that
    would otherwise skip the CORS preflight and reach a loopback API."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "use application/json")


@router.post(
    "/chat",
    dependencies=[Depends(require_api_token), Depends(require_json)],
    response_model=ChatResponse,
    responses={502: {"description": "every model on the route failed"}},
)
async def chat(body: ChatRequest, request: Request) -> Any:
    service: ChatService = request.app.state.hub.service
    try:
        classification = service.resolve_route(body)
    except UnknownRouteError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown route {exc.args[0]!r}") from None

    if body.stream:

        async def gen() -> AsyncIterator[bytes]:
            async for event in service.stream(body, source="api", classification=classification):
                yield sse_encode(event)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        return await service.complete(body, source="api")
    except ChatFailedError as exc:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content=exc.event.model_dump(mode="json"),
        )
