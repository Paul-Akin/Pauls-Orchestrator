"""Inter-device events: protocol models, validation, and the in-process bus.

The pydantic models in :mod:`hub.events.generated` are generated from
``protocol/schemas/*.json`` (the source of truth). Do not edit them by hand;
run ``uv run datamodel-codegen``.
"""

from hub.events.generated.command_envelope_v1 import CommandEnvelope
from hub.events.generated.device_status_v1 import DeviceStatus
from hub.events.generated.event_envelope_v1 import EventEnvelope

__all__ = ["CommandEnvelope", "DeviceStatus", "EventEnvelope"]
