"""Device-facing endpoints and transport lifecycle.

* ``POST /events/{device_id}``: HTTP webhook (Tasker, Home Assistant). Same
  envelope and validation as MQTT; device token as Bearer auth.
* ``GET /devices``: declared devices and MQTT presence.
* ``WS /ws/audio/{device_id}``: stub for the stage-5 CoreS3 audio stream.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket, status

from hub.api.auth import bearer_from_request, device_token_ok, require_api_token
from hub.events.bus import EventBus, log_event
from hub.events.validate import InvalidMessageError, ProtocolValidator
from hub.transports.mqtt import MqttTransport

if TYPE_CHECKING:
    from hub.api.app import HubState

log = structlog.get_logger(__name__)
router = APIRouter()

WS_TRY_AGAIN_LATER = 1013
WS_POLICY_VIOLATION = 1008


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI, state: HubState, *, mqtt_enabled: bool) -> AsyncIterator[None]:
    validator = ProtocolValidator()
    bus = EventBus()
    bus.subscribe("*", log_event)
    known = [d.id for d in state.config.hub.devices]
    mqtt = MqttTransport(state.config.hub.mqtt, state.settings, validator, bus, known)
    app.state.validator = validator
    app.state.bus = bus
    app.state.mqtt = mqtt if mqtt_enabled else None
    task: asyncio.Task[None] | None = None
    if mqtt_enabled:
        task = asyncio.create_task(mqtt.run(), name="mqtt")
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _known_device(request: Request, device_id: str) -> bool:
    return any(d.id == device_id for d in request.app.state.hub.config.hub.devices)


@router.post("/events/{device_id}", status_code=status.HTTP_202_ACCEPTED)
async def post_event(device_id: str, request: Request) -> dict[str, Any]:
    state: HubState = request.app.state.hub
    if not _known_device(request, device_id) or not device_token_ok(
        state.settings, device_id, bearer_from_request(request)
    ):
        # Same response for unknown device and bad token: don't confirm which ids exist.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid device credentials")
    body = await request.body()
    validator: ProtocolValidator = request.app.state.validator
    try:
        env = validator.parse_event(body, device_id=device_id)
    except InvalidMessageError as exc:
        log.warning("webhook_event_rejected", device_id=device_id, reason=exc.reason)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.reason) from None
    bus: EventBus = request.app.state.bus
    accepted = await bus.publish(env)
    return {"accepted": accepted, "duplicate": not accepted, "type": env.type}


@router.get("/devices", dependencies=[Depends(require_api_token)])
async def list_devices(request: Request) -> dict[str, Any]:
    state: HubState = request.app.state.hub
    mqtt: MqttTransport | None = request.app.state.mqtt
    presence = mqtt.presence if mqtt else {}
    return {
        "mqtt_enabled": mqtt is not None,
        "devices": [
            {
                "id": d.id,
                "kind": d.kind,
                "description": d.description,
                "has_token": d.id in state.settings.device_tokens,
                "presence": presence[d.id].state if d.id in presence else "unknown",
            }
            for d in state.config.hub.devices
        ],
    }


@router.websocket("/ws/audio/{device_id}")
async def audio_ws(websocket: WebSocket, device_id: str) -> None:
    """Stub. Authenticates, then closes with 1013 (try again later).

    Token via ``Authorization: Bearer`` (ESP32 clients) or ``?token=`` (browsers,
    which cannot set WebSocket headers).
    """
    state: HubState = websocket.app.state.hub
    header = websocket.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else None
    token = token or websocket.query_params.get("token")
    known = any(d.id == device_id for d in state.config.hub.devices)
    if not known or not device_token_ok(state.settings, device_id, token):
        await websocket.close(code=WS_POLICY_VIOLATION, reason="invalid device credentials")
        return
    await websocket.accept()
    await websocket.send_json(
        {"type": "not_implemented", "detail": "audio streaming arrives in stage 5"}
    )
    await websocket.close(code=WS_TRY_AGAIN_LATER, reason="audio not implemented yet")
