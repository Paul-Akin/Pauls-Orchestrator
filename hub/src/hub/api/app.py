"""FastAPI application factory and hub wiring (composition root)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from hub import __name__ as _pkg
from hub.api import chat, devices, system
from hub.config import AppConfig, check_runtime, load_config
from hub.context.builder import ContextBuilder, PassThroughContextBuilder
from hub.memory import MemoryProvider, NoOpMemoryProvider
from hub.paths import repo_root
from hub.pipeline import ChatService
from hub.providers.registry import ProviderRegistry
from hub.routing.classifier import KeywordClassifier
from hub.routing.router import Router
from hub.settings import Settings
from hub.usage.store import UsageStore

log = structlog.get_logger(__name__)
VERSION = "0.1.0"


@dataclass
class HubState:
    settings: Settings
    config: AppConfig
    registry: ProviderRegistry
    service: ChatService
    usage: UsageStore
    memory: MemoryProvider
    warnings: list[str] = field(default_factory=list)


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else repo_root() / path


def build_state(
    settings: Settings,
    config: AppConfig,
    *,
    registry: ProviderRegistry | None = None,
    context_builder: ContextBuilder | None = None,
    memory: MemoryProvider | None = None,
) -> HubState:
    warnings = check_runtime(config, settings)
    registry = registry or ProviderRegistry.from_settings(settings)
    usage = UsageStore(_resolve(config.hub.usage_db))
    routes = config.routes
    service = ChatService(
        routes=routes,
        router=Router(routes, registry),
        classifier=KeywordClassifier(routes),
        context_builder=context_builder or PassThroughContextBuilder(),
        usage_store=usage,
    )
    return HubState(
        settings=settings,
        config=config,
        registry=registry,
        service=service,
        usage=usage,
        memory=memory or NoOpMemoryProvider(),
        warnings=warnings,
    )


def create_app(
    settings: Settings | None = None,
    config: AppConfig | None = None,
    *,
    registry: ProviderRegistry | None = None,
    enable_mqtt: bool | None = None,
) -> FastAPI:
    settings = settings or Settings()
    config = config or load_config(settings.config_dir)
    state = build_state(settings, config, registry=registry)
    if enable_mqtt is not None:
        mqtt_on = enable_mqtt
    elif settings.mqtt_enabled_override is not None:
        mqtt_on = settings.mqtt_enabled_override
    else:
        mqtt_on = config.hub.mqtt.enabled

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        for w in state.warnings:
            log.warning("config_warning", detail=w)
        await state.registry.warm()
        avail = await state.registry.availability()
        log.info(
            "hub_started",
            version=VERSION,
            providers={p.value: a.available for p, a in avail.items()},
            usage_db=str(state.usage.path),
        )
        async with devices.lifespan(app, state, mqtt_enabled=mqtt_on):
            yield
        log.info("hub_stopped")

    app = FastAPI(
        title="hub",
        version=VERSION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.hub = state
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=config.hub.server.effective_allowed_hosts()
    )
    app.include_router(system.router)
    app.include_router(chat.router)
    app.include_router(devices.router)
    static_dir = Path(str(files(_pkg).joinpath("web/static")))
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="web")
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn --factory``."""
    return create_app()
