"""Opt-in live tests: one real call per provider, skipped unless its key is set.

    ANTHROPIC_API_KEY=... uv run pytest -m integration -k anthropic

Model IDs come from the shipped routes.yaml, so these also catch a retired model.
Each call asks for a handful of tokens to keep cost negligible.
"""

from __future__ import annotations

import os

import pytest

from hub.config import load_config
from hub.locality import ProviderName
from hub.paths import default_config_dir
from hub.providers.litellm_adapter import LiteLLMAdapter
from hub.settings import Settings
from hub.types import Message, ModelRequest

pytestmark = pytest.mark.integration

SETTINGS = Settings()
CFG = load_config(default_config_dir()).routes


def _first_model(provider: ProviderName) -> str:
    return next(a for a, s in CFG.models.items() if s.provider is provider)


async def _ping(adapter: LiteLLMAdapter, alias: str) -> None:
    req = ModelRequest(
        messages=[Message(role="user", content="Reply with the single word: pong")], max_tokens=16
    )
    chunks = [c async for c in adapter.stream(CFG.models[alias], req)]
    assert "".join(c.text for c in chunks).strip(), "empty answer"
    assert chunks[-1].usage is not None


@pytest.mark.skipif(SETTINGS.anthropic_api_key is None, reason="ANTHROPIC_API_KEY not set")
async def test_anthropic_live() -> None:
    a = LiteLLMAdapter(ProviderName.ANTHROPIC, api_key=SETTINGS.anthropic_api_key)
    await _ping(a, _first_model(ProviderName.ANTHROPIC))


@pytest.mark.skipif(SETTINGS.gemini_api_key is None, reason="GEMINI_API_KEY not set")
async def test_gemini_live() -> None:
    a = LiteLLMAdapter(ProviderName.GEMINI, api_key=SETTINGS.gemini_api_key)
    await _ping(a, _first_model(ProviderName.GEMINI))


@pytest.mark.skipif(SETTINGS.meta_api_key is None, reason="META_API_KEY not set")
async def test_meta_live() -> None:
    a = LiteLLMAdapter(ProviderName.META, api_key=SETTINGS.meta_api_key)
    await _ping(a, _first_model(ProviderName.META))


@pytest.mark.skipif(
    os.environ.get("HUB_TEST_OLLAMA") != "1", reason="set HUB_TEST_OLLAMA=1 with Ollama running"
)
async def test_ollama_live() -> None:
    a = LiteLLMAdapter(ProviderName.OLLAMA, api_key=None, api_base=SETTINGS.ollama_api_base)
    assert (await a.availability()).available
    await _ping(a, _first_model(ProviderName.OLLAMA))
