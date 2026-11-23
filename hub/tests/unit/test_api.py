"""HTTP API tests against the real app wired to fake providers."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hub.api.app import create_app
from hub.config import parse_config
from hub.locality import ProviderName
from hub.settings import Settings
from tests.fakes import FakeAdapter, registry

BASE = "http://127.0.0.1"
DEVICE_TOKEN = "dev-token-123"


def make_client(
    routes_dict: dict[str, Any],
    tmp_path: Path,
    *,
    adapters: dict[ProviderName, FakeAdapter] | None = None,
    env: dict[str, Any] | None = None,
    hub: dict[str, Any] | None = None,
) -> TestClient:
    hub_cfg = {
        "usage_db": str(tmp_path / "usage.sqlite3"),
        "devices": [{"id": "phone-paul", "kind": "android"}],
        **(hub or {}),
    }
    settings = Settings.model_validate(
        {"HUB_DEVICE_TOKENS": {"phone-paul": DEVICE_TOKEN}, **(env or {})}
    )
    app = create_app(
        settings,
        parse_config(hub_cfg, routes_dict),
        registry=registry(adapters or {}),
        enable_mqtt=False,
    )
    return TestClient(app, base_url=BASE)


@pytest.fixture
def client(routes_dict: dict[str, Any], tmp_path: Path) -> Iterator[TestClient]:
    with make_client(routes_dict, tmp_path) as c:
        yield c


def sse_events(body: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def chat_body(text: str = "hello", **kw: Any) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": text}], **kw}


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_providers(client: TestClient) -> None:
    data = client.get("/providers").json()
    names = {p["provider"] for p in data["providers"]}
    assert names == {"anthropic", "gemini", "meta", "ollama"}
    private = next(r for r in data["routes"] if r["name"] == "private")
    assert private["local_only"] is True


def test_chat_auto_returns_answer_and_metadata(client: TestClient) -> None:
    r = client.post("/chat", json=chat_body("check this beam", route="auto"))
    assert r.status_code == 200
    body = r.json()
    assert body["content"] == "answer from claude-opus-5-5"
    meta = body["metadata"]
    assert meta["route"] == "engineering_reasoning"
    assert meta["model"] == "claude-opus-5-5"
    assert meta["usage"] == {"input_tokens": 10, "output_tokens": 3, "estimated": False}
    assert meta["cost"] == {"usd": None, "status": "price not configured"}
    usage = client.get("/usage/recent").json()["requests"]
    assert usage[0]["request_id"] == meta["request_id"]


def test_chat_streams_sse(client: TestClient) -> None:
    r = client.post("/chat", json=chat_body(route="default", stream=True))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = sse_events(r.text)
    assert [e for e, _ in events] == ["route", "attempt", "delta", "delta", "delta", "done"]
    assert events[-1][1]["metadata"]["model_alias"] == "claude"


def test_fallback_visible_in_response(routes_dict: dict[str, Any], tmp_path: Path) -> None:
    bad = FakeAdapter(ProviderName.ANTHROPIC, behavior="error")
    with make_client(routes_dict, tmp_path, adapters={ProviderName.ANTHROPIC: bad}) as c:
        meta = c.post("/chat", json=chat_body(route="default")).json()["metadata"]
    assert meta["fell_back"] is True
    assert meta["model_alias"] == "gemini"
    assert meta["attempts"][0]["outcome"] == "error"
    assert "auth" in meta["attempts"][0]["reason"]


def test_all_failed_returns_502(routes_dict: dict[str, Any], tmp_path: Path) -> None:
    bad = FakeAdapter(ProviderName.OLLAMA, behavior="error")
    with make_client(routes_dict, tmp_path, adapters={ProviderName.OLLAMA: bad}) as c:
        r = c.post("/chat", json=chat_body(route="private"))
    assert r.status_code == 502
    assert r.json()["type"] == "error"


def test_unknown_route_400(client: TestClient) -> None:
    assert client.post("/chat", json=chat_body(route="nope")).status_code == 400


def test_validation_422(client: TestClient) -> None:
    assert client.post("/chat", json={"messages": []}).status_code == 422
    bad_img = chat_body()
    bad_img["messages"][0]["images"] = [{"media_type": "image/png", "data_b64": "!!!"}]
    assert client.post("/chat", json=bad_img).status_code == 422


def test_non_json_content_type_rejected(client: TestClient) -> None:
    r = client.post(
        "/chat", content=json.dumps(chat_body()), headers={"content-type": "text/plain"}
    )
    assert r.status_code == 415


def test_foreign_host_header_rejected(client: TestClient) -> None:
    """DNS-rebinding defence: evil.example resolving to 127.0.0.1 must not work."""
    r = client.get("/health", headers={"host": "evil.example"})
    assert r.status_code == 400


def test_api_token_enforced_when_set(routes_dict: dict[str, Any], tmp_path: Path) -> None:
    with make_client(routes_dict, tmp_path, env={"HUB_API_TOKEN": "s3cret"}) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/").status_code == 200  # UI shell loads; it prompts for the token
        assert c.post("/chat", json=chat_body()).status_code == 401
        assert c.get("/providers").status_code == 401
        auth = {"authorization": "Bearer wrong"}
        assert c.get("/providers", headers=auth).status_code == 401
        auth = {"authorization": "Bearer s3cret"}
        assert c.post("/chat", json=chat_body(), headers=auth).status_code == 200


def test_web_ui_served(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ------------------------------------------------------------------ devices


def event(**over: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "device_id": "phone-paul",
        "ts": "2026-11-23T14:05:09Z",
        "type": "phone.notification",
        "payload": {"app": "Messages"},
        **over,
    }


def test_webhook_accepts_valid_event(client: TestClient) -> None:
    auth = {"authorization": f"Bearer {DEVICE_TOKEN}"}
    r = client.post("/events/phone-paul", json=event(), headers=auth)
    assert r.status_code == 202
    assert r.json() == {"accepted": True, "duplicate": False, "type": "phone.notification"}


def test_webhook_dedupes_by_id(client: TestClient) -> None:
    auth = {"authorization": f"Bearer {DEVICE_TOKEN}"}
    e = event(id="3f2b8c1e-5d4a-4f6b-9c2d-1a2b3c4d5e6f")
    assert client.post("/events/phone-paul", json=e, headers=auth).json()["accepted"] is True
    assert client.post("/events/phone-paul", json=e, headers=auth).json()["duplicate"] is True


@pytest.mark.parametrize(
    ("device", "token"),
    [("phone-paul", None), ("phone-paul", "wrong"), ("unknown-dev", DEVICE_TOKEN)],
)
def test_webhook_auth(client: TestClient, device: str, token: str | None) -> None:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    assert client.post(f"/events/{device}", json=event(), headers=headers).status_code == 401


@pytest.mark.parametrize(
    "body",
    [event(ts="2026-11-23T14:05:09+01:00"), event(device_id="cores3-desk"), event(extra=1)],
)
def test_webhook_rejects_invalid(client: TestClient, body: dict[str, Any]) -> None:
    auth = {"authorization": f"Bearer {DEVICE_TOKEN}"}
    assert client.post("/events/phone-paul", json=body, headers=auth).status_code == 422


def test_devices_listing(client: TestClient) -> None:
    data = client.get("/devices").json()
    assert data["devices"][0]["id"] == "phone-paul"
    assert data["devices"][0]["has_token"] is True


def test_audio_ws_stub(client: TestClient) -> None:
    with client.websocket_connect(f"ws://127.0.0.1/ws/audio/phone-paul?token={DEVICE_TOKEN}") as ws:
        assert ws.receive_json()["type"] == "not_implemented"
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
        assert ei.value.code == 1013


@pytest.mark.parametrize("path", ["/ws/audio/phone-paul?token=x", "/ws/audio/nobody?token=x"])
def test_audio_ws_rejects_bad_credentials(client: TestClient, path: str) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as ei,
        client.websocket_connect(f"ws://127.0.0.1{path}") as ws,
    ):
        ws.receive_json()
    assert ei.value.code == 1008
