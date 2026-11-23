from __future__ import annotations

import json
from typing import Any

import pytest

from hub.config import MqttConfig
from hub.events import EventEnvelope
from hub.events.bus import EventBus
from hub.events.validate import InvalidMessageError, ProtocolValidator
from hub.settings import Settings
from hub.transports.mqtt import MqttTransport

V = ProtocolValidator()


def env(**over: Any) -> dict[str, Any]:
    base = {
        "schema_version": 1,
        "device_id": "phone-paul",
        "ts": "2026-11-23T14:05:09Z",
        "type": "phone.notification",
        "payload": {"x": 1},
    }
    return {**base, **over}


def raw(**over: Any) -> bytes:
    return json.dumps(env(**over)).encode()


class Sink:
    def __init__(self) -> None:
        self.events: list[EventEnvelope] = []

    async def __call__(self, e: EventEnvelope) -> None:
        self.events.append(e)


def transport() -> tuple[MqttTransport, Sink]:
    bus = EventBus()
    sink = Sink()
    bus.subscribe("*", sink)
    t = MqttTransport(
        MqttConfig(), Settings.model_validate({}), V, bus, ["phone-paul", "cores3-desk"]
    )
    return t, sink


async def test_valid_mqtt_event_published() -> None:
    t, sink = transport()
    await t.handle("hub/phone-paul/event/phone.notification", raw())
    assert len(sink.events) == 1
    assert t.dropped == 0


@pytest.mark.parametrize(
    ("topic", "payload", "reason"),
    [
        ("hub/phone-paul/event/phone.notification", b"{not json", "not valid JSON"),
        ("hub/phone-paul/event/phone.notification", b"\xff\xfe", "not valid JSON"),
        ("hub/phone-paul/event/phone.notification", raw(ts="2026-11-23T14:05:09+01:00"), "schema"),
        ("hub/phone-paul/event/phone.notification", raw(schema_version=2), "schema"),
        ("hub/stranger/event/phone.notification", raw(device_id="stranger"), "unknown device"),
        ("hub/cores3-desk/event/phone.notification", raw(), "device_id mismatch"),
        ("hub/phone-paul/event/voice.wake", raw(), "type mismatch"),
        ("hub/phone-paul/oops", raw(), "unexpected topic"),
        ("hub/phone-paul/event/phone.notification", b"x" * 70_000, "larger than"),
    ],
)
async def test_invalid_messages_dropped_never_raise(
    topic: str, payload: bytes, reason: str
) -> None:
    t, sink = transport()
    await t.handle(topic, payload)  # must not raise
    assert sink.events == []
    assert t.dropped == 1


async def test_device_presence_tracked() -> None:
    t, _ = transport()
    status = {
        "schema_version": 1,
        "device_id": "cores3-desk",
        "ts": "2026-11-23T14:00:00Z",
        "state": "online",
    }
    await t.handle("device/cores3-desk/status", json.dumps(status).encode())
    assert t.presence["cores3-desk"].state == "online"


async def test_bus_isolates_failing_handler_and_dedupes() -> None:
    bus = EventBus()
    sink = Sink()

    async def boom(_: EventEnvelope) -> None:
        raise RuntimeError("handler bug")

    bus.subscribe("phone.*", boom)
    bus.subscribe("phone.*", sink)
    bus.subscribe("voice.*", sink)
    e = V.parse(EventEnvelope, env(id="3f2b8c1e-5d4a-4f6b-9c2d-1a2b3c4d5e6f"))
    assert await bus.publish(e) is True
    assert await bus.publish(e) is False  # duplicate id
    assert len(sink.events) == 1


def test_parse_event_error_reason_has_no_payload_content() -> None:
    with pytest.raises(InvalidMessageError) as ei:
        V.parse_event(raw(payload="secret-ish text"), device_id="phone-paul")
    assert "secret-ish" not in ei.value.reason
