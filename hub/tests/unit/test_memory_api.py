from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.unit.test_api import make_client


def remember(c: TestClient, text: str, sensitivity: str = "personal", **kw: Any) -> str:
    r = c.post("/memory", json={"text": text, "sensitivity": sensitivity, **kw})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def test_remember_search_get_roundtrip(routes_dict: dict[str, Any], tmp_path: Path) -> None:
    with make_client(routes_dict, tmp_path) as c:
        nid = remember(c, "Preload M10 8.8 bolts to 75% of proof load.", title="Bolt preload",
                       tags=["fasteners"])  # fmt: skip
        assert nid.startswith("personal/")
        assert (tmp_path / "mem" / f"{nid}.md").is_file()

        results = c.get("/memory/search", params={"q": "bolt preload"}).json()["results"]
        assert results[0]["id"] == nid
        assert results[0]["title"] == "Bolt preload"

        note = c.get(f"/memory/notes/{nid}").json()
        assert note["tags"] == ["fasteners"]
        assert "75% of proof load" in note["text"]

        recent = c.get("/memory/recent").json()["notes"]
        assert recent[0]["id"] == nid
        stats = c.get("/memory/stats").json()
        assert stats["enabled"] is True
        assert stats["notes"] == 1


def test_sensitivity_cap_on_owner_endpoints(routes_dict: dict[str, Any], tmp_path: Path) -> None:
    with make_client(routes_dict, tmp_path) as c:
        nid = remember(c, "Supplier bank account 1234.", sensitivity="private")
        assert nid.startswith("private/")
        q = {"q": "supplier bank"}
        assert c.get("/memory/search", params=q).json()["results"]  # owner default: up to private
        capped = c.get("/memory/search", params={**q, "max_sensitivity": "personal"}).json()
        assert capped["results"] == []
        assert (
            c.get(f"/memory/notes/{nid}", params={"max_sensitivity": "personal"}).status_code == 404
        )


def test_chat_reports_memory_used(routes_dict: dict[str, Any], tmp_path: Path) -> None:
    with make_client(routes_dict, tmp_path) as c:
        nid = remember(c, "Preload M10 8.8 bolts to 75% of proof load.", title="Bolt preload")
        meta = c.post(
            "/chat", json={"messages": [{"role": "user", "content": "M10 bolt preload?"}],
                           "route": "default"},
        ).json()["metadata"]  # fmt: skip
        assert [m["id"] for m in meta["memory_used"]] == [nid]
        off = c.post(
            "/chat", json={"messages": [{"role": "user", "content": "M10 bolt preload?"}],
                           "route": "default", "use_memory": False},
        ).json()["metadata"]  # fmt: skip
        assert off["memory_used"] == []


def test_validation_and_auth(routes_dict: dict[str, Any], tmp_path: Path) -> None:
    with make_client(routes_dict, tmp_path, env={"HUB_API_TOKEN": "t"}) as c:
        assert c.get("/memory/stats").status_code == 401
        auth = {"authorization": "Bearer t"}
        assert c.post("/memory", json={"text": ""}, headers=auth).status_code == 422
        assert c.post("/memory", json={"text": "x", "sensitivity": "secret"},
                      headers=auth).status_code == 422  # fmt: skip
        r = c.post("/memory", content=json.dumps({"text": "x"}),
                   headers={**auth, "content-type": "text/plain"})  # fmt: skip
        assert r.status_code == 415
        assert c.get("/memory/notes/../../etc/passwd", headers=auth).status_code == 404


def test_reindex_picks_up_hand_written_files(routes_dict: dict[str, Any], tmp_path: Path) -> None:
    with make_client(routes_dict, tmp_path) as c:
        (tmp_path / "mem" / "public").mkdir(parents=True, exist_ok=True)
        (tmp_path / "mem" / "public" / "welds.md").write_text("# Welds\nFillet throat = 0.707 leg.")
        # The startup reindex runs in the background and may already have seen the
        # file, so assert the outcome rather than which call did the work.
        first = c.post("/memory/reindex", json={}).json()
        assert first["embed_error"] is None
        assert c.get("/memory/stats").json()["notes"] == 1
        assert c.get("/memory/search", params={"q": "fillet throat"}).json()["results"]
        again = c.post("/memory/reindex", json={}).json()
        assert (again["added"], again["updated"], again["removed"]) == (0, 0, 0)


def test_memory_disabled(routes_dict: dict[str, Any], tmp_path: Path) -> None:
    with make_client(routes_dict, tmp_path, hub={"memory": {"enabled": False}}) as c:
        assert c.get("/memory/stats").json() == {"enabled": False}
        assert c.get("/memory/search", params={"q": "x"}).status_code == 404
        r = c.post(
            "/chat", json={"messages": [{"role": "user", "content": "hi"}], "route": "default"}
        )
        assert r.json()["metadata"]["memory_used"] == []
