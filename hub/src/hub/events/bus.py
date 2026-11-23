"""In-process publish/subscribe for device and input events.

Every inbound source (MQTT, HTTP webhook, and later screenpipe, cameras, voice)
publishes validated :class:`EventEnvelope` objects here; consumers subscribe by
type glob (``phone.*``, ``*``). A failing handler is logged and isolated: it can
never crash the hub or starve other handlers.
"""

from __future__ import annotations

import fnmatch
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import structlog

from hub.events.generated.event_envelope_v1 import EventEnvelope

log = structlog.get_logger(__name__)

Handler = Callable[[EventEnvelope], Awaitable[None]]
Publish = Callable[[EventEnvelope], Awaitable[bool]]


class InputSource(Protocol):
    """Stage-4 extension point: a long-running producer of events.

    Implementations (screenpipe poller, camera snapshotter, ...) are started as
    background tasks at hub startup and call ``publish`` for each event.
    """

    name: str

    async def run(self, publish: Publish) -> None: ...


@dataclass(frozen=True)
class _Subscription:
    pattern: str
    handler: Handler


class EventBus:
    def __init__(self, dedupe_window: int = 1024) -> None:
        self._subs: list[_Subscription] = []
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._window = dedupe_window

    def subscribe(self, pattern: str, handler: Handler) -> None:
        self._subs.append(_Subscription(pattern, handler))

    def _is_duplicate(self, env: EventEnvelope) -> bool:
        if env.id is None:
            return False
        key = f"{env.device_id}:{env.id}"
        if key in self._seen:
            return True
        self._seen[key] = None
        if len(self._seen) > self._window:
            self._seen.popitem(last=False)
        return False

    async def publish(self, env: EventEnvelope) -> bool:
        """Deliver to matching handlers. Returns False if dropped as a duplicate."""
        if self._is_duplicate(env):
            log.debug("event_duplicate_dropped", device_id=env.device_id, event_type=env.type)
            return False
        for sub in self._subs:
            if not fnmatch.fnmatchcase(env.type, sub.pattern):
                continue
            try:
                await sub.handler(env)
            except Exception as exc:
                log.error(
                    "event_handler_failed",
                    pattern=sub.pattern,
                    event_type=env.type,
                    device_id=env.device_id,
                    error=type(exc).__name__,
                )
        return True


async def log_event(env: EventEnvelope) -> None:
    """Default subscriber: record that an event arrived (metadata only, no payload)."""
    log.info("event_received", device_id=env.device_id, event_type=env.type, event_id=env.id)
