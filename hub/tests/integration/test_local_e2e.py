"""End-to-end through the REAL LiteLLM adapter against a loopback fake Ollama.

No traffic leaves 127.0.0.1, so this runs in CI by default. Cloud adapters are
tripwires, so the test also proves the private route never reaches them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from hub.api.app import create_app
from hub.config import parse_config
from hub.locality import CLOUD_PROVIDERS, ProviderName
from hub.providers.litellm_adapter import LiteLLMAdapter
from hub.providers.registry import ProviderRegistry
from hub.settings import Settings
from tests.fakes import CloudTripwire
from tests.integration.fake_ollama import FakeOllama


def test_private_route_end_to_end(routes_dict: dict[str, Any], tmp_path: Path) -> None:
    with FakeOllama() as ollama:
        # Status probes allowed (startup log), but any stream() call fails the test.
        wires = {p: CloudTripwire(p, allow_status=True) for p in CLOUD_PROVIDERS}
        reg = ProviderRegistry(
            {
                **wires,  # type: ignore[dict-item]
                ProviderName.OLLAMA: LiteLLMAdapter(
                    ProviderName.OLLAMA, api_key=None, api_base=ollama.url
                ),
            }
        )
        cfg = parse_config({"usage_db": str(tmp_path / "u.sqlite3")}, routes_dict)
        settings = Settings.model_validate({"OLLAMA_API_BASE": ollama.url})
        app = create_app(settings, cfg, registry=reg, enable_mqtt=False)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            r = client.post(
                "/chat",
                json={"messages": [{"role": "user", "content": "secret"}], "route": "private"},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["content"].startswith("Local model reply")
        meta = body["metadata"]
        assert meta["locality"] == "local"
        assert meta["usage"] == {"input_tokens": 12, "output_tokens": 9, "estimated": False}
        assert ollama.requests[0]["model"] == "llama3.2"
        assert ollama.requests[0]["messages"][-1]["content"] == "secret"
        assert all(w.touched == 0 for w in wires.values())
