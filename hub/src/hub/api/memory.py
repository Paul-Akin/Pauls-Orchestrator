"""Owner-facing memory endpoints (same API-token rules as /chat).

These serve YOU (web UI, CLI), so reads default to the full range up to
``private``. External clients go through MCP instead (hub/mcp), which is capped
at ``memory.mcp.max_sensitivity`` and can never read private notes.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from hub.api.auth import require_api_token
from hub.api.chat import require_json
from hub.memory.protocol import MemoryItem, NewMemoryItem, Sensitivity
from hub.memory.store import LocalMemoryProvider

router = APIRouter(prefix="/memory", dependencies=[Depends(require_api_token)])

SensitivityName = Literal["public", "personal", "private"]


class RememberBody(BaseModel):
    text: Annotated[str, Field(min_length=1, max_length=100_000)]
    title: Annotated[str, Field(max_length=200)] | None = None
    sensitivity: SensitivityName = "personal"
    tags: Annotated[list[Annotated[str, Field(max_length=50)]], Field(max_length=20)] = []


def _memory(request: Request) -> LocalMemoryProvider:
    mem: LocalMemoryProvider | None = request.app.state.hub.memory
    if mem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "memory is disabled (memory.enabled)")
    return mem


def _item_json(item: MemoryItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "title": item.title,
        "heading": item.heading,
        "text": item.text,
        "sensitivity": item.sensitivity.label,
        "tags": list(item.tags),
        "created": item.created_at.isoformat(),
        "source": item.source,
        "score": item.score,
    }


def _cap(name: str) -> Sensitivity:
    parsed = Sensitivity.parse(name)
    assert parsed is not None  # guaranteed by the Literal type
    return parsed


@router.get("/stats")
async def stats(request: Request) -> dict[str, Any]:
    mem: LocalMemoryProvider | None = request.app.state.hub.memory
    if mem is None:
        return {"enabled": False}
    return {"enabled": True, **(await mem.stats())}


@router.get("/search")
async def search(
    request: Request,
    q: Annotated[str, Query(min_length=1, max_length=2000)],
    k: Annotated[int, Query(ge=1, le=50)] = 5,
    max_sensitivity: SensitivityName = "private",
) -> dict[str, Any]:
    items = await _memory(request).search(q, k=k, max_sensitivity=_cap(max_sensitivity))
    return {"results": [_item_json(i) for i in items]}


@router.get("/recent")
async def recent(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    max_sensitivity: SensitivityName = "private",
) -> dict[str, Any]:
    rows = await _memory(request).recent(limit=limit, max_sensitivity=_cap(max_sensitivity))
    return {"notes": [{"id": i, "title": t, "created": c} for i, t, c in rows]}


@router.get("/notes/{note_id:path}")
async def get_note(
    request: Request, note_id: str, max_sensitivity: SensitivityName = "private"
) -> dict[str, Any]:
    item = await _memory(request).get(note_id, max_sensitivity=_cap(max_sensitivity))
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "note not found")
    return _item_json(item)


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_json)])
async def remember(request: Request, body: RememberBody) -> dict[str, Any]:
    note_id = await _memory(request).put(
        NewMemoryItem(
            text=body.text,
            title=body.title,
            sensitivity=_cap(body.sensitivity),
            tags=tuple(t.strip() for t in body.tags if t.strip()),
            source="api",
        )
    )
    return {"id": note_id, "sensitivity": body.sensitivity}


@router.post("/reindex", dependencies=[Depends(require_json)])
async def reindex(request: Request) -> dict[str, Any]:
    r = await _memory(request).reindex()
    return {
        "added": r.scan.added,
        "updated": r.scan.updated,
        "removed": r.scan.removed,
        "unchanged": r.scan.unchanged,
        "embedded": r.embedded,
        "pending_embedding": r.pending,
        "embed_error": r.embed_error,
        "warnings": r.warnings,
    }
