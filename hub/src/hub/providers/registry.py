"""Builds one adapter per provider from :class:`~hub.settings.Settings`."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from hub.config import RoutesConfig
from hub.locality import ProviderName
from hub.providers.base import Availability, ProviderAdapter
from hub.providers.litellm_adapter import LiteLLMAdapter
from hub.settings import Settings


@dataclass(frozen=True)
class ProviderStatus:
    provider: ProviderName
    available: bool
    detail: str
    models: list[str]


class ProviderRegistry:
    def __init__(self, adapters: Mapping[ProviderName, ProviderAdapter]) -> None:
        self._adapters = dict(adapters)

    @classmethod
    def from_settings(cls, settings: Settings) -> ProviderRegistry:
        return cls(
            {
                ProviderName.ANTHROPIC: LiteLLMAdapter(
                    ProviderName.ANTHROPIC, api_key=settings.anthropic_api_key
                ),
                ProviderName.GEMINI: LiteLLMAdapter(
                    ProviderName.GEMINI, api_key=settings.gemini_api_key
                ),
                ProviderName.META: LiteLLMAdapter(ProviderName.META, api_key=settings.meta_api_key),
                ProviderName.OLLAMA: LiteLLMAdapter(
                    ProviderName.OLLAMA, api_key=None, api_base=settings.ollama_api_base
                ),
            }
        )

    def get(self, name: ProviderName) -> ProviderAdapter:
        return self._adapters[name]

    async def warm(self) -> None:
        """Import LiteLLM off the event loop at startup so the first chat isn't slow."""
        if any(isinstance(a, LiteLLMAdapter) for a in self._adapters.values()):
            from hub.providers.litellm_adapter import _load_litellm

            await asyncio.to_thread(_load_litellm)

    async def availability(self) -> dict[ProviderName, Availability]:
        names = list(self._adapters)
        results = await asyncio.gather(*(self._adapters[n].availability() for n in names))
        return dict(zip(names, results, strict=True))

    async def status(self, routes: RoutesConfig) -> list[ProviderStatus]:
        avail = await self.availability()
        return [
            ProviderStatus(
                provider=name,
                available=a.available,
                detail=a.detail,
                models=[alias for alias, s in routes.models.items() if s.provider is name],
            )
            for name, a in avail.items()
        ]
