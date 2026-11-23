"""Structured logging (structlog) with a secret/content redaction safety net.

Policy: INFO-level events carry metadata only (route, model, token counts,
latency, cost). Prompt and completion text are never logged at any level by
hub code; the redactor below is a second line of defence in case a field with
a sensitive name slips into an event.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "token",
        "password",
        "secret",
        "messages",
        "prompt",
        "content",
        "completion",
        "text",
        "data_b64",
    }
)
# Common key shapes: sk-..., sk-ant-..., AIza..., long bearer tokens.
_KEY_PATTERN = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|AIza[0-9A-Za-z_\-]{20,}|Bearer\s+\S{12,})")


def redact(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = "[redacted]"
        elif isinstance(event_dict[key], str):
            event_dict[key] = _KEY_PATTERN.sub("[redacted]", event_dict[key])
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
    logging.basicConfig(level=level, stream=sys.stderr, format="%(levelname)s %(name)s %(message)s")
    for noisy in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
