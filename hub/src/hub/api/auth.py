"""Bearer-token checks for the HTTP API and devices.

* API token (``HUB_API_TOKEN``): optional on loopback, mandatory on LAN binds
  (enforced at startup by :func:`hub.config.check_runtime`). When set, every
  endpoint except ``/health`` and the static UI requires it.
* Device tokens (``HUB_DEVICE_TOKENS``): one per declared device; used for the
  event webhook and audio WebSocket (and as the device's MQTT password).

Comparisons use :func:`secrets.compare_digest` to avoid timing leaks.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from hub.settings import Settings


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def _matches(expected: str, presented: str | None) -> bool:
    return presented is not None and secrets.compare_digest(
        expected.encode("utf-8"), presented.encode("utf-8")
    )


def api_token_ok(settings: Settings, authorization: str | None) -> bool:
    if settings.hub_api_token is None or not settings.hub_api_token.get_secret_value():
        return True
    return _matches(settings.hub_api_token.get_secret_value(), _bearer(authorization))


def device_token_ok(settings: Settings, device_id: str, presented: str | None) -> bool:
    expected = settings.device_tokens.get(device_id)
    if expected is None or not expected.get_secret_value():
        return False
    return _matches(expected.get_secret_value(), presented)


async def require_api_token(request: Request) -> None:
    settings: Settings = request.app.state.hub.settings
    if not api_token_ok(settings, request.headers.get("authorization")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def bearer_from_request(request: Request) -> str | None:
    return _bearer(request.headers.get("authorization"))


class BearerTokenASGI:
    """Wraps a mounted ASGI app (the MCP endpoint) with the same API-token rule."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]
            }
            if not api_token_ok(self.settings, headers.get("authorization")):
                response = JSONResponse(
                    {"detail": "missing or invalid API token"},
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
