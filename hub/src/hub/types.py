"""Request/response data types shared across the API, router and providers."""

from __future__ import annotations

import base64
import binascii
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 20 MiB decoded per image; generous for screenshots, small enough to bound memory.
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class Locality(StrEnum):
    """Where a model call executes. Decided by code, never by user config."""

    LOCAL = "local"
    CLOUD = "cloud"


def locality_permitted(ceiling: Locality, actual: Locality) -> bool:
    """True if a call executing at ``actual`` is allowed under ``ceiling``.

    LOCAL ceiling admits only LOCAL; CLOUD ceiling admits both.
    """
    return ceiling is Locality.CLOUD or actual is Locality.LOCAL


class ImageInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    media_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
    data_b64: str = Field(repr=False)

    @field_validator("data_b64")
    @classmethod
    def _valid_base64(cls, v: str) -> str:
        try:
            raw = base64.b64decode(v, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("data_b64 is not valid base64") from exc
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
        return v

    def data_url(self) -> str:
        return f"data:{self.media_type};base64,{self.data_b64}"


class Message(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str
    images: list[ImageInput] = Field(default_factory=list)


class ChatRequest(BaseModel):
    """Inbound request from any interface (REST, CLI, MQTT, voice)."""

    messages: Annotated[list[Message], Field(min_length=1)]
    route: str = "auto"
    stream: bool = False
    max_tokens: Annotated[int, Field(gt=0)] | None = None
    temperature: Annotated[float, Field(ge=0.0, le=2.0)] | None = None
    use_memory: bool = True
    """Set false to answer without consulting the memory store."""

    @property
    def has_images(self) -> bool:
        return any(m.images for m in self.messages)


class MemoryRef(BaseModel):
    """A memory chunk that was injected into the prompt (shown to the user)."""

    id: str
    title: str
    heading: str
    sensitivity: str
    score: float


class ModelRequest(BaseModel):
    """What is actually sent to a model, after the ContextBuilder has run."""

    messages: list[Message]
    max_tokens: int | None = None
    temperature: float | None = None
    use_memory: bool = True
    memory_used: list[MemoryRef] = Field(default_factory=list)
    """Filled by the ContextBuilder; reported in ChatMetadata, never sent as such."""

    @property
    def has_images(self) -> bool:
        return any(m.images for m in self.messages)


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    estimated: bool = False
    """True when the provider did not report usage and we counted locally."""


class CostStatus(StrEnum):
    OK = "ok"
    PRICE_NOT_CONFIGURED = "price not configured"
    USAGE_UNAVAILABLE = "usage unavailable"


class Cost(BaseModel):
    usd: float | None
    status: CostStatus


class AttemptOutcome(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


class AttemptRecord(BaseModel):
    model_alias: str
    provider: str
    model: str
    outcome: AttemptOutcome
    reason: str | None = None
    latency_ms: int | None = None


class ChatMetadata(BaseModel):
    request_id: str
    route: str
    route_reason: str
    model_alias: str
    provider: str
    model: str
    locality: Locality
    usage: Usage | None
    cost: Cost
    fell_back: bool
    attempts: list[AttemptRecord]
    latency_ms: int
    memory_used: list[MemoryRef] = Field(default_factory=list)


class ChatResponse(BaseModel):
    content: str
    metadata: ChatMetadata


# --- Streaming events (one SSE event each; ``type`` is the SSE event name) ---


class RouteChosenEvent(BaseModel):
    type: Literal["route"] = "route"
    request_id: str
    route: str
    reason: str


class AttemptEvent(BaseModel):
    type: Literal["attempt"] = "attempt"
    model_alias: str
    provider: str
    model: str


class DeltaEvent(BaseModel):
    type: Literal["delta"] = "delta"
    text: str


class DoneEvent(BaseModel):
    type: Literal["done"] = "done"
    metadata: ChatMetadata


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    request_id: str
    message: str
    attempts: list[AttemptRecord]


StreamEvent = RouteChosenEvent | AttemptEvent | DeltaEvent | DoneEvent | ErrorEvent
