from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from protocol.models import PROTOCOL_VERSION


def build_health_router(
    *,
    server_version: str,
    llm_router: Any,
    hub: Any,
    config: Any,
    embedding_provider: Any,
    model_store_mode: str,
    mcp_state: Optional[Dict[str, Any]] = None,
) -> APIRouter:
    router = APIRouter()
    state = mcp_state or {}

    @router.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "server_version": server_version,
                "protocol_version": PROTOCOL_VERSION,
                "active_profile": llm_router.active_profile,
                "privacy_mode": hub.privacy_mode,
                "memory_backend": config.memory.backend,
                "embedding_enabled": embedding_provider.enabled,
                "model_store_mode": model_store_mode,
                "mcp_starting": state.get("starting", False),
            }
        )

    @router.get("/")
    async def index() -> JSONResponse:
        return JSONResponse(
            {
                "service": "qimingxing",
                "version": server_version,
                "status": "ok",
                "ui": "native",
            }
        )

    return router
