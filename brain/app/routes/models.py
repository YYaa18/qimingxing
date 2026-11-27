from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from brain.app.routes._deps import RouteDeps
from brain.model_discovery import discover_provider_models


def build_models_router(*, deps: RouteDeps) -> APIRouter:
    router = APIRouter()

    @router.post("/api/models/discover")
    async def discover_models(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in deps.loopback_hosts:
            raise HTTPException(
                status_code=403, detail="Model discovery is restricted to localhost."
            )
        if deps.model_store is None:
            raise HTTPException(status_code=503, detail="model_store_unavailable")
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc
        provider_id = str(body.get("provider_id") or "").strip()
        provider = deps.model_store.provider(provider_id)
        if provider is None:
            raise HTTPException(status_code=404, detail="provider_not_found")
        api_key = str(body.get("api_key") or "").strip()
        if not api_key or "****" in api_key:
            api_key = str(provider.get("api_key") or "")
        try:
            discovered = await discover_provider_models(
                provider_id=provider_id,
                protocol=str(provider.get("protocol") or "openai_compatible"),
                base_url=str(provider.get("base_url") or ""),
                api_key=api_key,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            api_message = ""
            try:
                payload = exc.response.json()
                if isinstance(payload, dict):
                    api_message = str((payload.get("error") or {}).get("message") or "")
            except Exception:
                api_message = exc.response.text[:200]
            base_url = str(provider.get("base_url") or "")
            if provider_id == "gemini" or "generativelanguage.googleapis.com" in base_url:
                hint = "请确认 Key 来自 Google AI Studio，并已启用 Generative Language API。"
            elif provider_id == "deepseek":
                hint = "请检查 DeepSeek API Key 是否有效、账户是否可用。"
            else:
                hint = "请检查 API Key 与 Base URL 是否正确。"
            detail = f"刷新模型失败（HTTP {status}）"
            if api_message:
                detail += f"：{api_message}"
            detail += f" {hint}"
            safe = deps.hub.config.redact_secrets(detail)
            if api_key and len(api_key) >= 4:
                safe = safe.replace(api_key, "***REDACTED***")
            raise HTTPException(status_code=502, detail=safe) from exc
        except Exception as exc:
            safe = deps.hub.config.redact_secrets(f"{type(exc).__name__}: {exc}")
            raise HTTPException(status_code=502, detail=safe) from exc
        count = deps.model_store.upsert_discovered(provider_id, discovered)
        deps.model_store.apply_to_config(deps.hub.config)
        await deps.hub.apply_config_update(deps.hub.config)
        return JSONResponse(
            {
                "status": "ok",
                "count": count,
                "models": discovered,
            }
        )

    return router
