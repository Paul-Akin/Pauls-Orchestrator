"""Hub side of the MQTT transport. Topic tree: protocol/mqtt-topics.md.

Inbound handling is split from the network loop so it can be unit-tested
without a broker: :meth:`MqttTransport.handle` takes (topic, payload) and never
raises. Every rejection is logged with a reason and the message is dropped.

Authorization is layered:

* Broker: username/password per device, plus an ACL so a device can only
  publish under ``hub/<its username>/...`` (deploy/mosquitto/acl).
* Hub: the topic's device_id must be a declared device, and must equal the
  envelope's device_id; the topic's type must equal the envelope's type.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from dataclasses import dataclass

import aiomqtt
import structlog

from hub.config import MqttConfig
from hub.events.bus import EventBus
from hub.events.generated.command_envelope_v1 import CommandEnvelope
from hub.events.generated.device_status_v1 import DeviceStatus
from hub.events.validate import InvalidMessageError, ProtocolValidator, utc_now_iso
from hub.settings import Settings

log = structlog.get_logger(__name__)

EVENT_SUBSCRIPTION = "hub/+/event/+"
STATUS_SUBSCRIPTION = "device/+/status"
HUB_STATUS_TOPIC = "hub/status"


@dataclass
class Presence:
    state: str
    ts: str
    fw_version: str | None = None


def hub_status_payload(state: str) -> str:
    return json.dumps(
        {"schema_version": 1, "device_id": "hub", "ts": utc_now_iso(), "state": state}
    )


class MqttTransport:
    def __init__(
        self,
        cfg: MqttConfig,
        settings: Settings,
        validator: ProtocolValidator,
        bus: EventBus,
        known_devices: Iterable[str],
    ) -> None:
        self._cfg = cfg
        self._settings = settings
        self._validator = validator
        self._bus = bus
        self._known = frozenset(known_devices)
        self._client: aiomqtt.Client | None = None
        self.presence: dict[str, Presence] = {}
        self.dropped = 0

    # ------------------------------------------------------------------ inbound

    async def handle(self, topic: str, payload: bytes) -> None:
        """Process one inbound message. Never raises."""
        try:
            await self._handle(topic, payload)
        except InvalidMessageError as exc:
            self._drop(topic, exc.reason)
        except Exception as exc:  # defence in depth: nothing from the wire crashes the hub
            self._drop(topic, f"internal error {type(exc).__name__}")

    async def _handle(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        if len(parts) == 4 and parts[0] == "hub" and parts[2] == "event":
            device_id, type_ = parts[1], parts[3]
            self._require_known(device_id)
            env = self._validator.parse_event(payload, device_id=device_id, type_=type_)
            await self._bus.publish(env)
        elif len(parts) == 3 and parts[0] == "device" and parts[2] == "status":
            device_id = parts[1]
            self._require_known(device_id)
            status = self._validator.parse(DeviceStatus, payload)
            if status.device_id != device_id:
                raise InvalidMessageError("device_id mismatch between topic and status")
            self.presence[device_id] = Presence(status.state, status.ts, status.fw_version)
            log.info("device_presence", device_id=device_id, state=status.state)
        else:
            raise InvalidMessageError("unexpected topic shape")

    def _require_known(self, device_id: str) -> None:
        if device_id not in self._known:
            raise InvalidMessageError(f"unknown device {device_id!r}")

    def _drop(self, topic: str, reason: str) -> None:
        self.dropped += 1
        log.warning("mqtt_message_dropped", topic=topic[:128], reason=reason)

    # ------------------------------------------------------------------ outbound

    async def send_command(self, cmd: CommandEnvelope) -> None:
        if cmd.device_id not in self._known:
            raise ValueError(f"unknown device {cmd.device_id!r}")
        if self._client is None:
            raise RuntimeError("MQTT not connected")
        await self._client.publish(
            f"device/{cmd.device_id}/cmd/{cmd.type}",
            cmd.model_dump_json(exclude_none=True),
            qos=1,
        )

    # ------------------------------------------------------------------ loop

    async def run(self) -> None:
        """Connect, subscribe and process forever, reconnecting with backoff."""
        backoff = 1.0
        password = self._settings.mqtt_password
        while True:
            try:
                async with aiomqtt.Client(
                    self._cfg.host,
                    self._cfg.port,
                    username=self._settings.mqtt_username,
                    password=password.get_secret_value() if password else None,
                    identifier=self._cfg.client_id,
                    keepalive=self._cfg.keepalive_s,
                    will=aiomqtt.Will(
                        HUB_STATUS_TOPIC, hub_status_payload("offline"), qos=1, retain=True
                    ),
                ) as client:
                    self._client = client
                    await client.publish(
                        HUB_STATUS_TOPIC, hub_status_payload("online"), qos=1, retain=True
                    )
                    await client.subscribe(EVENT_SUBSCRIPTION, qos=1)
                    await client.subscribe(STATUS_SUBSCRIPTION, qos=1)
                    log.info("mqtt_connected", host=self._cfg.host, port=self._cfg.port)
                    backoff = 1.0
                    async for message in client.messages:
                        payload = message.payload
                        if not isinstance(payload, bytes | bytearray):
                            self._drop(message.topic.value, "non-bytes payload")
                            continue
                        await self.handle(message.topic.value, bytes(payload))
            except aiomqtt.MqttError as exc:
                self._client = None
                log.warning("mqtt_disconnected", error=str(exc)[:200], retry_in_s=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
