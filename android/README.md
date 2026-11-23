# Android (placeholder)

Phase 1: **Tasker / Home Assistant configs**, no custom app. Phase 2 (maybe): a
Kotlin companion app.

## Sending an event from Tasker today

HTTP Request action:

* Method: `POST`
* URL: `http://<hub>:8765/events/phone-paul` (over Tailscale; requires
  `server.allow_lan` and `HUB_API_TOKEN` on the hub)
* Headers: `Authorization: Bearer <phone-paul token from HUB_DEVICE_TOKENS>`,
  `Content-Type: application/json`
* Body:

```json
{"schema_version":1,"device_id":"phone-paul","ts":"<UTC timestamp>",
 "type":"phone.notification","payload":{"app":"<app name>","title":"<title>"}}
```

`ts` must be UTC with a literal `Z` suffix, for example `2026-11-23T14:05:09Z`.
Build it in a Tasker variable first (format the current time in UTC as
`yyyy-MM-dd'T'HH:mm:ss'Z'`). The hub answers `202` on success and `422` with a
reason if the envelope is invalid. See `protocol/mqtt-topics.md` and
`protocol/schemas/`.
