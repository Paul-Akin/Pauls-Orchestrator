"""Shared fixtures. Tests never touch the network: all providers are fakes."""

from __future__ import annotations

import copy
from typing import Any

import pytest

BASE_ROUTES: dict[str, Any] = {
    "models": {
        "claude": {"provider": "anthropic", "model": "claude-opus-5-5", "vision": True},
        "gemini": {"provider": "gemini", "model": "gemini-3.1-pro-preview", "vision": True},
        "muse": {"provider": "meta", "model": "muse-spark-1.3", "vision": True},
        "local": {
            "provider": "ollama",
            "model": "llama3.2",
            "price": {"input_per_mtok": 0, "output_per_mtok": 0},
        },
    },
    "routes": {
        "default": {"primary": "claude", "fallback": ["gemini", "local"]},
        "engineering_reasoning": {"primary": "claude", "fallback": ["gemini"]},
        "long_context": {"primary": "gemini", "fallback": ["claude"]},
        "cheap_bulk": {"primary": "muse", "fallback": ["local"]},
        "private": {"primary": "local", "fallback": [], "local_only": True},
    },
    "classifier": {
        "default_route": "default",
        "rules": [
            {"route": "long_context", "min_chars": 1000},
            {"route": "engineering_reasoning", "any": ["beam", "buckling", "nx open"]},
            {"route": "private", "any": ["password"]},
        ],
    },
}


@pytest.fixture
def routes_dict() -> dict[str, Any]:
    return copy.deepcopy(BASE_ROUTES)
