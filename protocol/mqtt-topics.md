# MQTT topic tree

The hub is the only party every device talks to. Devices never subscribe to
each other's topics; the broker ACL (`deploy/mosquitto/acl`) enforces this.

| Topic | Direction | Retained | Schema | Notes |
|---|---|---|---|---|
| `hub/{device_id}/event/{type}` | device → hub | no | `event-envelope.v1.json` | QoS 1. `{device_id}` and `{type}` must equal the envelope's fields. |
| `device/{device_id}/cmd/{type}` | hub → device | no | `command-envelope.v1.json` | QoS 1. Devices subscribe to `device/{own id}/cmd/#`. |
| `device/{device_id}/status` | device → all | **yes** | `device-status.v1.json` | Publish `online` after connect; register `offline` as Last Will. |
| `hub/status` | hub → all | **yes** | `device-status.v1.json` (`device_id: "hub"`) | Hub's own presence; LWT = `offline`. |

## Identifiers

* `device_id`: `^[a-z0-9][a-z0-9-]{1,62}$`, e.g. `cores3-desk`, `phone-paul`.
  It is also the device's **MQTT username**, which is what lets the broker
  ACL pin each device to its own subtree (`%u` pattern).
* `type`: dotted lowercase, `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$`, e.g.
  `phone.notification`, `voice.wake`, `camera.snapshot_ready`, `tts.play`.
  Dots are legal in MQTT topic levels; slashes are not allowed in `type`.

## Envelope rules

* `ts` is ISO-8601 **UTC with a literal `Z`**: `2026-11-23T14:05:09.123Z`.
  Offsets such as `+05:00` are rejected. (ESP32: sync SNTP before publishing.)
* `schema_version` is the envelope's major version (currently `1`).
* `id` (optional UUID) lets the hub drop QoS-1 redeliveries.
* Max message size: 64 KiB. Larger payloads (audio, images) go over
  WebSocket/HTTP and are *referenced* from an event.

## Hub behaviour on bad input

Every inbound message is validated against `protocol/schemas` (jsonschema with
format checking) and then parsed into the generated pydantic model. Messages
that fail, come from an undeclared device, or whose topic disagrees with the
envelope are **logged with a reason and dropped**. They never crash the hub,
and the payload content is never logged.

## Example (mosquitto clients)

```bash
mosquitto_pub -h 127.0.0.1 -u phone-paul -P "$TOKEN" -q 1 \
  -t hub/phone-paul/event/phone.notification \
  -m '{"schema_version":1,"device_id":"phone-paul","ts":"2026-11-23T14:05:09Z","type":"phone.notification","payload":{"app":"Messages"}}'
```

## Same envelope over HTTP

Devices that can't speak MQTT (Tasker, Home Assistant automations) POST the same
envelope to `POST /events/{device_id}` with `Authorization: Bearer <device token>`.
