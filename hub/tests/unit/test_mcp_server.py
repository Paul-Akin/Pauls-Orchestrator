"""MCP server: real protocol round-trips via the SDK's in-process Client.

Tripwire for invariant 1: PRIVATE notes are never returned over MCP, by search,
by direct id, or in the recent list.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from hub.mcp_server import build_mcp_server
from hub.memory.index import MemoryIndex
from hub.memory.protocol import NewMemoryItem, Sensitivity
from hub.memory.store import LocalMemoryProvider
from tests.fakes import FakeEmbedder
from tests.unit.test_api import make_client

SECRET = "9876-5432"


async def seeded(tmp_path: Path) -> tuple[LocalMemoryProvider, dict[str, str]]:
    mem = LocalMemoryProvider(tmp_path / "mem", MemoryIndex(tmp_path / "i.sqlite3"), FakeEmbedder())
    ids = {
        "bank": await mem.put(NewMemoryItem(text=f"Bolt supplier bank account {SECRET}.",
                                            source="t", sensitivity=Sensitivity.PRIVATE,
                                            title="Supplier bank")),
        "preload": await mem.put(NewMemoryItem(text="Preload M10 8.8 bolts to 75% of proof load.",
                                               source="t", sensitivity=Sensitivity.PERSONAL,
                                               title="Bolt preload")),
        "grades": await mem.put(NewMemoryItem(text="Bolt grade 8.8: 800 MPa UTS.", source="t",
                                              sensitivity=Sensitivity.PUBLIC, title="Bolt grades")),
    }  # fmt: skip
    return mem, ids


async def test_tools_are_listed_read_only(tmp_path: Path) -> None:
    mem, _ = await seeded(tmp_path)
    async with Client(build_mcp_server(mem, max_sensitivity=Sensitivity.PERSONAL)) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {"search_memory", "get_memory", "recent_memories"}
    for t in tools.values():
        assert t.annotations is not None
        assert t.annotations.read_only_hint is True
        assert t.annotations.destructive_hint is False


async def test_search_never_returns_private(tmp_path: Path) -> None:
    mem, ids = await seeded(tmp_path)
    async with Client(build_mcp_server(mem, max_sensitivity=Sensitivity.PERSONAL)) as c:
        r = await c.call_tool("search_memory", {"query": "bolt supplier bank account", "k": 10})
    assert r.is_error is False
    hits = r.structured_content["result"]  # type: ignore[index]
    assert ids["preload"] in {h["id"] for h in hits}
    assert ids["bank"] not in {h["id"] for h in hits}
    assert SECRET not in str(r.content)


async def test_get_private_looks_like_not_found(tmp_path: Path) -> None:
    mem, ids = await seeded(tmp_path)
    async with Client(build_mcp_server(mem, max_sensitivity=Sensitivity.PERSONAL)) as c:
        ok = await c.call_tool("get_memory", {"id": ids["preload"]})
        hidden = await c.call_tool("get_memory", {"id": ids["bank"]})
        missing = await c.call_tool("get_memory", {"id": "personal/nope"})
        escape = await c.call_tool("get_memory", {"id": "../../../etc/passwd"})
    assert ok.is_error is False
    assert ok.structured_content is not None
    assert "75% of proof load" in ok.structured_content["text"]
    for r in (hidden, missing, escape):
        assert r.is_error is True
        assert SECRET not in str(r.content)
    assert hidden.content == missing.content  # byte-identical: existence is not revealed


async def test_public_cap(tmp_path: Path) -> None:
    mem, ids = await seeded(tmp_path)
    async with Client(build_mcp_server(mem, max_sensitivity=Sensitivity.PUBLIC)) as c:
        recent = await c.call_tool("recent_memories", {"limit": 10})
    assert [n["id"] for n in recent.structured_content["result"]] == [ids["grades"]]  # type: ignore[index]


async def test_refuses_private_cap(tmp_path: Path) -> None:
    mem, _ = await seeded(tmp_path)
    with pytest.raises(ValueError, match="never serve private"):
        build_mcp_server(mem, max_sensitivity=Sensitivity.PRIVATE)


async def test_argument_validation(tmp_path: Path) -> None:
    mem, _ = await seeded(tmp_path)
    async with Client(build_mcp_server(mem, max_sensitivity=Sensitivity.PERSONAL)) as c:
        r = await c.call_tool("search_memory", {"query": "x", "k": 500})
    assert r.is_error is True


# ------------------------------------------------------------------ HTTP mount


def _rpc(method: str, params: dict[str, Any], rid: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}


HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def test_http_mount_requires_token_when_set(routes_dict: dict[str, Any], tmp_path: Path) -> None:
    init = _rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "t", "version": "0"}})  # fmt: skip
    with make_client(routes_dict, tmp_path, env={"HUB_API_TOKEN": "tok"}) as c:
        assert c.post("/mcp/", json=init, headers=HEADERS).status_code == 401
        r = c.post("/mcp/", json=init, headers={**HEADERS, "authorization": "Bearer tok"})
        assert r.status_code == 200, r.text


def test_http_mount_rejects_foreign_origin(routes_dict: dict[str, Any], tmp_path: Path) -> None:
    init = _rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "t", "version": "0"}})  # fmt: skip
    with make_client(routes_dict, tmp_path) as c:
        r = c.post("/mcp/", json=init, headers={**HEADERS, "origin": "https://evil.example"})
        assert r.status_code in (400, 403)


def test_mcp_disabled(routes_dict: dict[str, Any], tmp_path: Path) -> None:
    with make_client(routes_dict, tmp_path, hub={"memory": {"mcp": {"enabled": False}}}) as c:
        # Falls through to the static mount: not an MCP endpoint.
        r = c.post("/mcp/", json={}, headers=HEADERS)
        assert r.status_code in (404, 405)
