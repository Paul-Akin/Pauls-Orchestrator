"""Health, provider status and usage endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from hub.api.auth import require_api_token

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Liveness. Unauthenticated, reveals nothing sensitive."""
    return {"status": "ok", "version": request.app.version}


@router.get("/providers", dependencies=[Depends(require_api_token)])
async def providers(request: Request) -> dict[str, Any]:
    state = request.app.state.hub
    statuses = await state.registry.status(state.config.routes)
    models = state.config.routes.models
    return {
        "providers": [
            {
                "provider": s.provider.value,
                "available": s.available,
                "detail": s.detail,
                "models": [
                    {
                        "alias": alias,
                        "model": models[alias].model,
                        "vision": models[alias].vision,
                        "price_configured": models[alias].price.configured,
                    }
                    for alias in s.models
                ],
            }
            for s in statuses
        ],
        "routes": [
            {
                "name": name,
                "primary": r.primary,
                "fallback": r.fallback,
                "local_only": r.local_only,
                "description": r.description,
            }
            for name, r in state.config.routes.routes.items()
        ],
        "warnings": state.warnings,
    }


@router.get("/usage/recent", dependencies=[Depends(require_api_token)])
async def usage_recent(
    request: Request, limit: int = Query(default=20, ge=1, le=500)
) -> dict[str, Any]:
    return {"requests": request.app.state.hub.usage.recent(limit)}
