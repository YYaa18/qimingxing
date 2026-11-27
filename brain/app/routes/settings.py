from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from brain.app.routes._deps import RouteDeps
from brain.config import get_sanitized_settings, save_settings_to_yaml
from brain.llm_router import probe_provider_connectivity
from brain.model_config_store import (
    ModelStoreConflictError,
    ModelStoreValidationError,
)


def build_settings_router(*, deps: RouteDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/config/settings")
    async def get_settings(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in deps.loopback_hosts:
            raise HTTPException(
                status_code=403, detail="Settings access is restricted to localhost."
            )
        return JSONResponse(get_sanitized_settings(deps.hub.config))

    @router.post("/api/config/settings")
    async def update_settings(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in deps.loopback_hosts:
            raise HTTPException(
                status_code=403, detail="Settings updates are restricted to localhost."
            )
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object.")

        try:
            if deps.model_store is not None:
                if isinstance(body.get("providers"), dict):
                    deps.model_store.apply_provider_patch(body["providers"])
                if isinstance(body.get("model_routing"), dict):
                    deps.model_store.apply_routing_patch(body["model_routing"])
                body = {
                    key: value
                    for key, value in body.items()
                    if key not in ("providers", "model_routing", "profiles")
                }
            fresh_cfg = save_settings_to_yaml(
                body, config_path=deps.settings_config_path
            )
            if deps.model_store is not None:
                deps.model_store.apply_to_config(fresh_cfg)
        except ModelStoreConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ModelStoreValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            safe_err = deps.hub.config.redact_secrets(f"{type(exc).__name__}: {exc}")
            raise HTTPException(status_code=400, detail=f"保存配置失败: {safe_err}") from exc

        await deps.hub.apply_config_update(fresh_cfg)
        return JSONResponse(
            {
                "status": "ok",
                "settings": get_sanitized_settings(fresh_cfg),
            }
        )

    @router.post("/api/config/test_provider")
    async def test_provider(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in deps.loopback_hosts:
            raise HTTPException(
                status_code=403,
                detail="Provider connectivity probes are restricted to localhost.",
            )
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object.")

        res = await probe_provider_connectivity(
            deps.hub.config,
            provider_id=str(body.get("provider_id") or body.get("name") or "").strip(),
            base_url=str(body.get("base_url") or "").strip(),
            api_key=str(body.get("api_key") or "").strip(),
            protocol=str(body.get("protocol") or "openai_compatible").strip(),
            model=str(body.get("model") or "").strip(),
            transport=deps.probe_transport,
        )
        return JSONResponse(res)

    return router
