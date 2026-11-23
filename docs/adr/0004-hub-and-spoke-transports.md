# ADR 0004: Hub-and-spoke topology with an MQTT / HTTP / WebSocket split

* Status: accepted
* Date: 2026-11-23

## Context

Future devices: a CoreS3 voice satellite (ESP32-S3), a Windows tray agent, an
Android phone (Tasker / Home Assistant first), cameras (go2rtc), screen capture
(screenpipe). They differ wildly in language, power, connectivity and uptime.

## Decision: topology

**Hub and spoke.** Devices talk only to the hub, never to each other. The hub
owns state (memory, routing, logs); devices are thin. This keeps auth, policy
and logging in one place, and means a compromised device can only reach its own
topics.

## Decision: transports by traffic shape

| Traffic | Transport | Why |
|---|---|---|
| Small async events and commands, presence | **MQTT** (Mosquitto) | Tiny client on ESP32, QoS 1 delivery, retained presence and Last Will built in, broker-side per-device ACLs |
| Request/response (chat, status, webhooks) | **HTTP REST** (+ SSE for streaming) | Universal; Tasker/HA/scripts can call it; SSE streams tokens one way |
| Continuous binary streams (audio) | **WebSocket** `/ws/audio/{device_id}` | Full duplex, low overhead per frame, works through the same HTTP port. Stubbed in stage 1 |

MQTT messages are capped at 64 KiB; large blobs (audio, images) go over
WebSocket/HTTP and are referenced from an event.

## Contract

`protocol/` is the single source of truth: JSON Schemas, topic tree and a
versioned changelog. The hub's pydantic models are **generated** from the
schemas (`datamodel-codegen`), CI fails if they are stale, and contract tests run
every example fixture through both jsonschema and pydantic. Firmware and Android
validate against the same files.

Invalid inbound messages are logged with a reason and dropped. They never crash
the hub, and payload content is never logged.

## Security

* Broker: no anonymous access; one account per device (username = `device_id`,
  password = device token); ACL patterns pin each device to `hub/%u/event/+`,
  `device/%u/cmd/+`, `device/%u/status`.
* Hub: re-checks that the topic's device is declared and matches the envelope.
* HTTP: binds to `127.0.0.1` unless `allow_lan` is set **and** `HUB_API_TOKEN`
  exists; device endpoints use per-device bearer tokens.
* LAN only. MQTT on 1883 is unencrypted on the LAN in stage 1; enable the 8883
  TLS listener (commented in `mosquitto.conf`) before adding untrusted networks.
* **Remote access via Tailscale, not port forwarding.** Install Tailscale on the
  hub host and the phone; reach `http://<hub-tailnet-name>:8765` (with
  `allow_lan` + token) or use `tailscale serve` to proxy the loopback port. Not
  implemented in stage 1, only documented.

## Consequences

* One more service to run (Mosquitto), handled by `docker-compose.yml`.
* Device clocks must be NTP-synced: `ts` must be UTC with a `Z` suffix.
* Adding a device kind means a new `device_id`, a token, and possibly new event
  types (additive protocol change).
