from __future__ import annotations

import html
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from brain.app.routes._deps import RouteDeps
from brain.config import (
    delete_mcp_server_in_yaml,
    get_sanitized_mcp_servers,
    load_config,
    upsert_mcp_server_in_yaml,
)


def build_mcp_router(*, deps: RouteDeps) -> APIRouter:
    router = APIRouter()

    def merged_mcp_server_views() -> List[Dict[str, Any]]:
        status_map = {s.name: asdict(s) for s in deps.mcp.list_statuses()}
        out: List[Dict[str, Any]] = []
        for item in get_sanitized_mcp_servers(deps.hub.config):
            st = status_map.get(item["name"], {})
            merged = dict(item)
            merged["status"] = st.get(
                "status", "disabled" if not item["enabled"] else "offline"
            )
            merged["tools"] = st.get("tools", [])
            merged["error"] = st.get("error", "")
            merged["has_oauth_token"] = bool(
                st.get("has_oauth_token", deps.oauth.has_saved_token(item["name"]))
            )
            out.append(merged)
        return out

    @router.post("/api/mcp/reload")
    async def reload_mcp_servers(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in deps.loopback_hosts:
            raise HTTPException(
                status_code=403, detail="MCP reload is restricted to localhost."
            )

        new_servers = None
        if not deps.config_injected:
            try:
                fresh_cfg = load_config()
                new_servers = fresh_cfg.mcp_servers
            except Exception as exc:
                safe_err = deps.config.redact_secrets(
                    f"{type(exc).__name__}: {exc}"
                )
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to load config for MCP reload: {safe_err}",
                ) from exc

        statuses = await deps.mcp.reload(
            deps.hub.tool_registry, servers=new_servers
        )
        return JSONResponse(
            {
                "status": "ok",
                "mcp_servers": [asdict(s) for s in statuses],
            }
        )

    @router.get("/api/mcp/servers")
    async def list_mcp_servers(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in deps.loopback_hosts:
            raise HTTPException(
                status_code=403,
                detail="MCP config access is restricted to localhost.",
            )
        return JSONResponse({"status": "ok", "servers": merged_mcp_server_views()})

    @router.post("/api/mcp/servers")
    async def upsert_mcp_server(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in deps.loopback_hosts:
            raise HTTPException(
                status_code=403,
                detail="MCP config updates are restricted to localhost.",
            )
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object.")

        try:
            fresh_cfg = upsert_mcp_server_in_yaml(
                body, config_path=deps.settings_config_path
            )
        except Exception as exc:
            safe_err = deps.hub.config.redact_secrets(
                f"{type(exc).__name__}: {exc}"
            )
            raise HTTPException(
                status_code=400, detail=f"保存 MCP 配置失败: {safe_err}"
            ) from exc

        deps.hub.config = fresh_cfg
        await deps.mcp.reload(deps.hub.tool_registry, servers=fresh_cfg.mcp_servers)
        return JSONResponse({"status": "ok", "servers": merged_mcp_server_views()})

    @router.delete("/api/mcp/servers/{server_name}")
    async def remove_mcp_server(server_name: str, request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in deps.loopback_hosts:
            raise HTTPException(
                status_code=403, detail="MCP deletion is restricted to localhost."
            )
        clear_auth = str(request.query_params.get("clear_auth", "true")).lower() != "false"
        fresh_cfg = delete_mcp_server_in_yaml(
            server_name, config_path=deps.settings_config_path
        )
        if clear_auth:
            deps.oauth.clear_saved_auth(server_name)
        deps.hub.config = fresh_cfg
        await deps.mcp.reload(deps.hub.tool_registry, servers=fresh_cfg.mcp_servers)
        return JSONResponse({"status": "ok", "servers": merged_mcp_server_views()})

    @router.post("/api/mcp/servers/{server_name}/authorize")
    async def authorize_mcp_server(server_name: str, request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in deps.loopback_hosts:
            raise HTTPException(
                status_code=403,
                detail="MCP authorization is restricted to localhost.",
            )
        result = await deps.mcp.start_oauth_authorization(
            server_name, deps.hub.tool_registry
        )
        return JSONResponse(
            {
                **result,
                "servers": merged_mcp_server_views(),
            }
        )

    @router.get("/api/mcp/oauth/callback")
    async def mcp_oauth_callback(
        code: Optional[str] = None,
        state: Optional[str] = None,
        iss: Optional[str] = None,
        error: Optional[str] = None,
        error_description: Optional[str] = None,
    ) -> HTMLResponse:
        ok, message = deps.oauth.complete_callback(
            code=code,
            state=state,
            iss=iss,
            error=error,
            error_description=error_description,
        )
        safe_title = "启明星 MCP 授权成功 ✨" if ok else "启明星 MCP 授权未完成"
        badge_col = "#38bdf8" if ok else "#f97316"
        body_html = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{html.escape(safe_title)}</title></head>"
            "<body style='background:#0f172a;color:#f1f5f9;font-family:-apple-system,sans-serif;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0;'>"
            "<div style='background:#1e293b;border:1px solid rgba(255,255,255,0.12);"
            "border-radius:14px;padding:28px 32px;max-width:460px;text-align:center;'>"
            f"<h2 style='color:{badge_col};margin-top:0;'>{html.escape(safe_title)}</h2>"
            f"<p style='line-height:1.6;color:#cbd5e1;'>{html.escape(message)}</p>"
            "</div></body></html>"
        )
        return HTMLResponse(content=body_html, status_code=200 if ok else 400)

    return router
