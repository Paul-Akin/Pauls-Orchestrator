# CoreS3 voice satellite (placeholder, stage 5)

Future C++ firmware for the M5Stack CoreS3 (ESP32-S3), using M5Unified on ESP-IDF
(or Arduino-ESP32).

Planned responsibilities:

* On-device wake word; stream microphone audio to the hub over
  `WS /ws/audio/{device_id}` (STT with faster-whisper runs on the hub).
* Play TTS audio returned by the hub (Piper on the hub).
* MQTT: publish `voice.*` events to `hub/{device_id}/event/{type}`, subscribe to
  `device/{device_id}/cmd/#`, publish a retained `device/{device_id}/status` with
  an `offline` Last Will.

Contract: `protocol/` (schemas, topic tree). Credentials: MQTT username =
`device_id`, password = the device's token in the hub's `HUB_DEVICE_TOKENS`.
Sync SNTP before publishing: `ts` must be UTC with a `Z` suffix.
