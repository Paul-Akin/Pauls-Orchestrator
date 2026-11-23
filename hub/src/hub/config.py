"""Non-secret configuration loaded from ``hub/config/{hub,routes}.yaml``.

All structural rules are enforced at load time, so a bad config fails fast at
startup instead of at 2 a.m. when a fallback finally fires. The most important
rule: a ``local_only`` route may reference only models whose *static* locality
is LOCAL (see :mod:`hub.locality`).
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from hub.locality import ProviderName, is_private_endpoint, model_locality, static_locality
from hub.settings import Settings
from hub.types import Locality

RESERVED_ROUTE_NAMES = frozenset({"auto"})
PLACEHOLDER_MARKERS = ("<set in config>", "changeme", "TODO")


class ConfigError(Exception):
    """Raised for any invalid configuration. Message is meant for humans."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- routes


class Price(_Strict):
    """USD per 1,000,000 tokens. ``None`` means "not configured" (never guessed)."""

    input_per_mtok: Annotated[float, Field(ge=0)] | None = None
    output_per_mtok: Annotated[float, Field(ge=0)] | None = None

    @property
    def configured(self) -> bool:
        return self.input_per_mtok is not None and self.output_per_mtok is not None


class ModelSpec(_Strict):
    alias: str
    provider: ProviderName
    model: Annotated[str, Field(min_length=1)]
    vision: bool = False
    timeout_s: Annotated[float, Field(gt=0)] = 60.0
    """Max seconds to the first chunk, and max idle gap between chunks."""
    price: Price = Price()

    @field_validator("model")
    @classmethod
    def _not_placeholder(cls, v: str) -> str:
        if any(marker.lower() in v.lower() for marker in PLACEHOLDER_MARKERS):
            raise ValueError(f"model id {v!r} is a placeholder; set a real model id")
        return v

    @property
    def static_locality(self) -> Locality:
        return static_locality(self.provider, self.model)


class RouteSpec(_Strict):
    primary: str
    fallback: list[str] = Field(default_factory=list)
    local_only: bool = False
    """If true, the route may never execute a call outside this machine/LAN."""
    deadline_s: Annotated[float, Field(gt=0)] = 120.0
    """Budget for the whole fallback search (until the first chunk arrives)."""
    description: str = ""

    @property
    def chain(self) -> list[str]:
        return [self.primary, *self.fallback]

    @property
    def locality_ceiling(self) -> Locality:
        return Locality.LOCAL if self.local_only else Locality.CLOUD


class ClassifierRule(_Strict):
    """Heuristic rule for ``route: auto``. All given conditions must hold."""

    route: str
    any_keywords: list[str] = Field(default_factory=list, alias="any")
    min_chars: Annotated[int, Field(gt=0)] | None = None
    has_images: bool | None = None

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _has_condition(self) -> ClassifierRule:
        if not self.any_keywords and self.min_chars is None and self.has_images is None:
            raise ValueError(f"classifier rule for {self.route!r} has no conditions")
        return self


class ClassifierConfig(_Strict):
    default_route: str = "default"
    rules: list[ClassifierRule] = Field(default_factory=list)


class RoutesConfig(_Strict):
    models: dict[str, ModelSpec]
    routes: dict[str, RouteSpec]
    classifier: ClassifierConfig = ClassifierConfig()

    @model_validator(mode="before")
    @classmethod
    def _inject_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("models"), dict):
            data = dict(data)
            data["models"] = {
                alias: ({"alias": alias, **spec} if isinstance(spec, dict) else spec)
                for alias, spec in data["models"].items()
            }
        return data

    @model_validator(mode="after")
    def _check_references(self) -> RoutesConfig:
        if not self.models:
            raise ValueError("no models defined")
        if "default" not in self.routes:
            raise ValueError("a route named 'default' is required")
        for name, route in self.routes.items():
            if name in RESERVED_ROUTE_NAMES:
                raise ValueError(f"route name {name!r} is reserved")
            chain = route.chain
            if len(set(chain)) != len(chain):
                raise ValueError(f"route {name!r} lists a model more than once: {chain}")
            for alias in chain:
                if alias not in self.models:
                    raise ValueError(f"route {name!r} references unknown model {alias!r}")
            if name == "private" and not route.local_only:
                raise ValueError("route 'private' must set local_only: true")
            if route.local_only:
                for alias in chain:
                    spec = self.models[alias]
                    if spec.static_locality is not Locality.LOCAL:
                        raise ValueError(
                            f"local_only route {name!r} references model {alias!r} "
                            f"({spec.provider}/{spec.model}) which is not local. "
                            "A local_only route must never be able to reach a cloud provider."
                        )
        known = set(self.routes)
        if self.classifier.default_route not in known:
            raise ValueError(f"classifier.default_route {self.classifier.default_route!r} unknown")
        for rule in self.classifier.rules:
            if rule.route not in known:
                raise ValueError(f"classifier rule references unknown route {rule.route!r}")
        return self


# --------------------------------------------------------------------------- hub


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ServerConfig(_Strict):
    host: str = "127.0.0.1"
    port: Annotated[int, Field(gt=0, lt=65536)] = 8765
    allow_lan: bool = False
    """Must be true to bind to anything other than loopback. Also requires HUB_API_TOKEN."""
    allowed_hosts: list[str] | None = None
    """Accepted HTTP Host headers (DNS-rebinding defence). Default: loopback names,
    or any host when ``allow_lan`` is true."""

    @model_validator(mode="after")
    def _lan_opt_in(self) -> ServerConfig:
        if not _is_loopback_host(self.host) and not self.allow_lan:
            raise ValueError(
                f"server.host={self.host!r} is not loopback; set server.allow_lan: true to opt in"
            )
        return self

    @property
    def is_loopback(self) -> bool:
        return _is_loopback_host(self.host)

    def effective_allowed_hosts(self) -> list[str]:
        if self.allowed_hosts is not None:
            return self.allowed_hosts
        if self.allow_lan:
            return ["*"]
        return ["127.0.0.1", "localhost", "[::1]", "::1"]


class MqttConfig(_Strict):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: Annotated[int, Field(gt=0, lt=65536)] = 1883
    client_id: str = "hub"
    keepalive_s: Annotated[int, Field(gt=0)] = 30


class DeviceConfig(_Strict):
    id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")]
    kind: str
    description: str = ""


class EmbeddingConfig(_Strict):
    """Embeddings always run on the local Ollama (OLLAMA_API_BASE): see docs/adr/0005."""

    model: Annotated[str, Field(min_length=1)] = "nomic-embed-text"
    timeout_s: Annotated[float, Field(gt=0)] = 30.0
    batch_size: Annotated[int, Field(gt=0, le=256)] = 32


class RetrievalConfig(_Strict):
    """Hybrid retrieval knobs. Formulas: hub/memory/retrieval.py and docs/adr/0005."""

    candidates: Annotated[int, Field(gt=0, le=200)] = 20
    """Hits taken from EACH retriever (keyword, vector) before fusion."""
    inject_max: Annotated[int, Field(gt=0, le=50)] = 6
    """Max chunks injected into one prompt."""
    rrf_k: Annotated[int, Field(gt=0)] = 60
    recency_half_life_days: Annotated[float, Field(gt=0)] = 180.0
    recency_floor: Annotated[float, Field(ge=0, le=1)] = 0.7
    min_cosine: Annotated[float, Field(ge=-1, le=1)] = 0.35
    token_budget: Annotated[int, Field(gt=0)] = 1500
    chunk_max_chars: Annotated[int, Field(ge=200)] = 1600


class McpConfig(_Strict):
    enabled: bool = True
    max_sensitivity: Literal["public", "personal"] = "personal"
    """Most sensitive memory MCP clients may read. 'private' is deliberately not allowed."""


class MemoryConfig(_Strict):
    enabled: bool = True
    dir: Path = Path("~/hub-memory")
    """Markdown notes: the source of truth. Keep it out of the repo; back it up."""
    index_db: Path = Path("data/memory_index.sqlite3")
    """Derived search index; safe to delete (rebuilt by reindex)."""
    watch: bool = True
    """Re-index automatically when files in ``dir`` change."""
    embedding: EmbeddingConfig = EmbeddingConfig()
    retrieval: RetrievalConfig = RetrievalConfig()
    mcp: McpConfig = McpConfig()


class HubConfig(_Strict):
    server: ServerConfig = ServerConfig()
    mqtt: MqttConfig = MqttConfig()
    memory: MemoryConfig = MemoryConfig()
    usage_db: Path = Path("data/usage.sqlite3")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["console", "json"] = "console"
    devices: list[DeviceConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_devices(self) -> HubConfig:
        ids = [d.id for d in self.devices]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate device ids")
        return self


class AppConfig(_Strict):
    hub: HubConfig
    routes: RoutesConfig


# --------------------------------------------------------------------------- loading


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data


def parse_config(hub: dict[str, Any], routes: dict[str, Any]) -> AppConfig:
    """Validate already-parsed YAML. Raises :class:`ConfigError` with a readable message."""
    try:
        return AppConfig.model_validate({"hub": hub, "routes": routes})
    except ValidationError as exc:
        lines = [f"  - {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ConfigError("invalid configuration:\n" + "\n".join(lines)) from exc


def load_config(config_dir: Path) -> AppConfig:
    return parse_config(
        _read_yaml(config_dir / "hub.yaml"),
        _read_yaml(config_dir / "routes.yaml"),
    )


def check_runtime(cfg: AppConfig, settings: Settings) -> list[str]:
    """Checks that need both YAML and secrets/env. Raises on fatal; returns warnings."""
    warnings: list[str] = []
    host = settings.host_override or cfg.hub.server.host
    if not _is_loopback_host(host) and settings.hub_api_token is None:
        raise ConfigError(
            f"refusing to bind to non-loopback host {host!r} without HUB_API_TOKEN set"
        )
    local_routes = [n for n, r in cfg.routes.routes.items() if r.local_only]
    if local_routes and not is_private_endpoint(settings.ollama_api_base):
        raise ConfigError(
            f"OLLAMA_API_BASE={settings.ollama_api_base!r} is not a loopback/private address, "
            f"so local_only routes {local_routes} could leave the network"
        )
    mem = cfg.hub.memory
    if mem.enabled:
        # Embedding a note sends its full text to the model, so it must be local.
        loc = model_locality(ProviderName.OLLAMA, mem.embedding.model, settings.ollama_api_base)
        if loc is not Locality.LOCAL:
            raise ConfigError(
                f"memory.embedding.model={mem.embedding.model!r} at OLLAMA_API_BASE="
                f"{settings.ollama_api_base!r} is not local; notes would leave the machine"
            )
    for device in cfg.hub.devices:
        if device.id not in settings.device_tokens:
            warnings.append(
                f"device {device.id!r} has no token in HUB_DEVICE_TOKENS; it cannot auth"
            )
    return warnings
