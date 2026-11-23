"""FastAPI application factory and hub wiring (composition root)."""

from __future__ import annotations

import asyncio
import contextlib
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
from hub.api import memory as memory_api  # aliased: create_app has a `memory` parameter
from hub.config import AppConfig, MemoryConfig, check_runtime, load_config
from hub.context.builder import ContextBuilder, PassThroughContextBuilder
from hub.context.memory_builder import MemoryContextBuilder
from hub.memory.embedder import OllamaEmbedder
from hub.memory.index import MemoryIndex
from hub.memory.retrieval import RetrievalParams
from hub.memory.store import LocalMemoryProvider
from hub.memory.watch import initial_reindex, watch_memory
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
    memory: LocalMemoryProvider | None
    """None when memory.enabled is false."""
    warnings: list[str] = field(default_factory=list)


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else repo_root() / path


def build_memory(settings: Settings, cfg: MemoryConfig) -> LocalMemoryProvider:
    """The default memory stack: ~/hub-memory notes, SQLite index, local Ollama embedder."""
    r = cfg.retrieval
    return LocalMemoryProvider(
        (settings.memory_dir_override or cfg.dir).expanduser(),
        MemoryIndex(_resolve(cfg.index_db)),
        OllamaEmbedder(
            settings.ollama_api_base,
            cfg.embedding.model,
            timeout_s=cfg.embedding.timeout_s,
            batch_size=cfg.embedding.batch_size,
        ),
        params=RetrievalParams(
            rrf_k=r.rrf_k,
            half_life_days=r.recency_half_life_days,
            recency_floor=r.recency_floor,
            min_cosine=r.min_cosine,
        ),
        candidates=r.candidates,
        chunk_max_chars=r.chunk_max_chars,
    )


def build_state(
    settings: Settings,
    config: AppConfig,
    *,
    registry: ProviderRegistry | None = None,
    context_builder: ContextBuilder | None = None,
    memory: LocalMemoryProvider | None = None,
) -> HubState:
    warnings = check_runtime(config, settings)
    registry = registry or ProviderRegistry.from_settings(settings)
    usage = UsageStore(_resolve(config.hub.usage_db))
    routes = config.routes
    mem_cfg = config.hub.memory
    if mem_cfg.enabled:
        memory = memory or build_memory(settings, mem_cfg)
        context_builder = context_builder or MemoryContextBuilder(memory, mem_cfg.retrieval)
    else:
        memory = None
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
        memory=memory,
        warnings=warnings,
    )


def create_app(
    settings: Settings | None = None,
    config: AppConfig | None = None,
    *,
    registry: ProviderRegistry | None = None,
    enable_mqtt: bool | None = None,
    memory: LocalMemoryProvider | None = None,
) -> FastAPI:
    settings = settings or Settings()
    config = config or load_config(settings.config_dir)
    state = build_state(settings, config, registry=registry, memory=memory)
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
            memory_dir=str(state.memory.root) if state.memory else None,
        )
        tasks: list[asyncio.Task[None]] = []
        if state.memory is not None:
            # In the background: embedding many notes must not delay startup.
            tasks.append(asyncio.create_task(initial_reindex(state.memory), name="mem-index"))
            if config.hub.memory.watch:
                tasks.append(asyncio.create_task(watch_memory(state.memory), name="mem-watch"))
        try:
            async with devices.lifespan(app, state, mqtt_enabled=mqtt_on):
                yield
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await t
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
    app.include_router(memory_api.router)
    static_dir = Path(str(files(_pkg).joinpath("web/static")))
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="web")
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn --factory``."""
    return create_app()
