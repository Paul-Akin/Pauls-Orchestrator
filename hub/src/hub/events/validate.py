"""Validation of inbound device messages against ``protocol/schemas``.

Two layers, deliberately redundant:

1. ``jsonschema`` (Draft 2020-12 with format checking) against the schema files,
   which are the cross-language source of truth (firmware and Android validate
   against the same files).
2. The generated pydantic model, which gives the hub a typed object.

The contract tests assert both layers agree on every fixture in
``protocol/examples``; if they ever disagree, the generated models are stale.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError

from hub.events.generated.command_envelope_v1 import CommandEnvelope
from hub.events.generated.device_status_v1 import DeviceStatus
from hub.events.generated.event_envelope_v1 import EventEnvelope
from hub.paths import schemas_dir

MAX_MESSAGE_BYTES = 64 * 1024

SCHEMA_FILES: dict[type[BaseModel], str] = {
    EventEnvelope: "event-envelope.v1.json",
    CommandEnvelope: "command-envelope.v1.json",
    DeviceStatus: "device-status.v1.json",
}

M = TypeVar("M", bound=BaseModel)


class InvalidMessageError(ValueError):
    """Message rejected. ``reason`` is safe to log (contains no payload content)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ProtocolValidator:
    def __init__(self, directory: Path | None = None) -> None:
        directory = directory or schemas_dir()
        self._validators: dict[type[BaseModel], Draft202012Validator] = {}
        for model, filename in SCHEMA_FILES.items():
            schema = json.loads((directory / filename).read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            self._validators[model] = Draft202012Validator(
                schema, format_checker=Draft202012Validator.FORMAT_CHECKER
            )

    def schema_errors(self, model: type[BaseModel], data: Any) -> list[str]:
        """All JSON Schema violations, as 'path: message' strings (payload values elided)."""
        errors = sorted(self._validators[model].iter_errors(data), key=lambda e: list(e.path))
        return [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.validator}" for e in errors]

    def parse(self, model: type[M], raw: bytes | str | dict[str, Any]) -> M:
        if isinstance(raw, bytes | str):
            if len(raw) > MAX_MESSAGE_BYTES:
                raise InvalidMessageError(f"message larger than {MAX_MESSAGE_BYTES} bytes")
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise InvalidMessageError(f"not valid JSON ({type(exc).__name__})") from None
        else:
            data = raw
        errors = self.schema_errors(model, data)
        if errors:
            raise InvalidMessageError("schema: " + "; ".join(errors[:5]))
        try:
            return model.model_validate(data)
        except ValidationError as exc:  # schema passed but model failed => drift
            raise InvalidMessageError(f"model drift: {exc.error_count()} error(s)") from None

    def parse_event(
        self, raw: bytes | str | dict[str, Any], *, device_id: str, type_: str | None = None
    ) -> EventEnvelope:
        """Parse an event and check it matches the channel it arrived on."""
        env = self.parse(EventEnvelope, raw)
        if env.device_id != device_id:
            raise InvalidMessageError(
                f"device_id mismatch: channel {device_id!r} vs envelope {env.device_id!r}"
            )
        if type_ is not None and env.type != type_:
            raise InvalidMessageError(f"type mismatch: channel {type_!r} vs envelope {env.type!r}")
        return env
