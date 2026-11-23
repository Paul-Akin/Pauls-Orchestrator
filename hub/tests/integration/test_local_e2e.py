"""End-to-end through the REAL LiteLLM adapter and REAL OllamaEmbedder against a
loopback fake Ollama. No traffic leaves 127.0.0.1, so this runs in CI by default.

Cloud adapters are tripwires, so the test also proves the private route (and
private memory) never reaches them.
"""

from __future__ import annotations

import time
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

PRIVATE_NOTE = """---
title: Supplier bank details
sensitivity: private
---
The bolt supplier's bank account number is 9876-5432.
"""


def test_private_route_with_private_memory_end_to_end(
    routes_dict: dict[str, Any], tmp_path: Path
) -> None:
    mem_dir = tmp_path / "mem"
    (mem_dir / "private").mkdir(parents=True)
    (mem_dir / "private" / "bank.md").write_text(PRIVATE_NOTE)

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
        hub_cfg = {
            "usage_db": str(tmp_path / "u.sqlite3"),
            "memory": {
                "dir": str(mem_dir),
                "index_db": str(tmp_path / "idx.sqlite3"),
                "watch": False,
                "embedding": {"model": "nomic-embed-text"},
            },
        }
        settings = Settings.model_validate({"OLLAMA_API_BASE": ollama.url})
        app = create_app(
            settings, parse_config(hub_cfg, routes_dict), registry=reg, enable_mqtt=False
        )
        with TestClient(app, base_url="http://127.0.0.1") as client:
            # Wait for the background initial reindex (embeds via the fake /api/embed).
            for _ in range(100):
                if client.get("/memory/stats").json().get("notes") == 1:
                    break
                time.sleep(0.05)
            r = client.post(
                "/chat",
                json={
                    "messages": [{"role": "user", "content": "what is the supplier bank account?"}],
                    "route": "private",
                },
            )
        assert r.status_code == 200, r.text
        body = r.json()
        meta = body["metadata"]
        assert body["content"].startswith("Local model reply")
        assert meta["locality"] == "local"
        assert meta["usage"] == {"input_tokens": 12, "output_tokens": 9, "estimated": False}
        assert [m["id"] for m in meta["memory_used"]] == ["private/bank"]
        assert meta["memory_used"][0]["sensitivity"] == "private"

        [chat] = ollama.requests_to("/api/chat")
        assert chat["model"] == "llama3.2"
        assert "9876-5432" in chat["messages"][0]["content"]  # injected memory block
        assert chat["messages"][-1]["content"] == "what is the supplier bank account?"
        embeds = ollama.requests_to("/api/embed")
        assert embeds
        assert {e["model"] for e in embeds} == {"nomic-embed-text"}
        assert all(w.touched == 0 for w in wires.values())
