from __future__ import annotations

import pytest
from hub.locality import ProviderName, is_ollama_cloud_model, is_private_endpoint, model_locality
from hub.types import Locality


@pytest.mark.parametrize(
    ("model", "cloud"),
    [
        ("llama3.2", False),
        ("qwen2.5:14b", False),
        ("gpt-oss:120b-cloud", True),
        ("glm-4.6:cloud", True),
        ("deepseek-v3.1:671b-CLOUD", True),
        ("cloud", True),
        ("cloudberry:7b", False),  # 'cloud' must be a delimited token
    ],
)
def test_ollama_cloud_tag(model: str, cloud: bool) -> None:
    assert is_ollama_cloud_model(model) is cloud


@pytest.mark.parametrize(
    ("url", "private"),
    [
        ("http://127.0.0.1:11434", True),
        ("http://localhost:11434", True),
        ("http://[::1]:11434", True),
        ("http://192.168.1.20:11434", True),
        ("http://10.0.0.5:11434", True),
        ("http://172.16.4.4:11434", True),
        ("http://100.101.102.103:11434", False),  # Tailscale CGNAT: fail closed
        ("http://8.8.8.8:11434", False),
        ("https://ollama.com", False),
        ("http://0.0.0.0:11434", False),
        ("not a url", False),
    ],
)
def test_private_endpoint(url: str, private: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    import hub.locality as loc

    def fake_resolve(host: str, *_: object) -> list[tuple[object, ...]]:
        table = {"localhost": "127.0.0.1", "ollama.com": "34.36.133.15"}
        if host not in table:
            raise OSError("no such host")
        return [(None, None, None, None, (table[host], 0))]

    loc._dns_cache.clear()
    monkeypatch.setattr(loc.socket, "getaddrinfo", fake_resolve)
    assert is_private_endpoint(url) is private


@pytest.mark.parametrize(
    "provider", [ProviderName.ANTHROPIC, ProviderName.GEMINI, ProviderName.META]
)
def test_cloud_providers_are_always_cloud(provider: ProviderName) -> None:
    # Even with a loopback api_base (e.g. a local proxy), the vendor is cloud.
    assert model_locality(provider, "x", "http://127.0.0.1:1") is Locality.CLOUD


def test_ollama_locality() -> None:
    assert (
        model_locality(ProviderName.OLLAMA, "llama3.2", "http://127.0.0.1:11434") is Locality.LOCAL
    )
    assert model_locality(ProviderName.OLLAMA, "llama3.2", "http://8.8.8.8:11434") is Locality.CLOUD
    assert (
        model_locality(ProviderName.OLLAMA, "x:cloud", "http://127.0.0.1:11434") is Locality.CLOUD
    )
    assert model_locality(ProviderName.OLLAMA, "llama3.2", None) is Locality.CLOUD
