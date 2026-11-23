# Windows tray agent (placeholder)

Future Python tray agent for the Windows workstation, added as a second member of
the repo's uv workspace.

Planned responsibilities:

* Quick-ask hotkey that sends to the hub's `POST /chat` and shows the reply with
  the "which model answered" badge.
* Forward desktop events (for example NX Open job finished, screenpipe triggers)
  as `event-envelope.v1` messages over MQTT.
* Presence via retained `device/{device_id}/status` with a Last Will.

Contract: `protocol/`. Talks only to the hub, never to other devices.
