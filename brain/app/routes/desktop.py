from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from brain.app.routes._deps import RouteDeps
from brain.tools.desktop_io import execute_desktop_action


def build_desktop_router(*, deps: RouteDeps) -> APIRouter:
    router = APIRouter()

    @router.post("/api/context/attach_paths")
    async def attach_paths(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in deps.loopback_hosts:
            raise HTTPException(
                status_code=403, detail="Path attachment is restricted to localhost."
            )

        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object.")

        raw_paths = body.get("paths") or []
        if not isinstance(raw_paths, list):
            raise HTTPException(status_code=400, detail="`paths` must be a list.")
        source = str(body.get("source") or "manual").strip() or "manual"
        append = bool(body.get("append", True))

        res = deps.hub.attach_context_paths(
            [str(p) for p in raw_paths], source=source, append=append
        )
        if res.accepted_paths:
            await deps.hub.broadcast_tool_notice(
                "batch_context",
                f"已接收 {len(res.accepted_paths)} 个本地路径（共 {res.total_entries} 条引用，来源: {source}）",
            )
        return JSONResponse(
            {
                "status": "ok",
                "source": res.source,
                "accepted": len(res.accepted_paths),
                "filtered": len(res.filtered_paths),
                "missing": len(res.missing_paths),
                "total_entries": res.total_entries,
                "pending_roots": deps.hub.batch_path_store.pending_count(),
                "accepted_paths": res.accepted_paths,
                "filtered_paths": res.filtered_paths,
                "manifest_preview": res.manifest_text,
            }
        )

    @router.post("/api/desktop/open_action")
    async def desktop_open_action(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in deps.loopback_hosts:
            raise HTTPException(
                status_code=403,
                detail="Desktop open actions are restricted to localhost.",
            )

        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object.")

        action = str(body.get("action") or "")
        path = str(body.get("path") or "")
        try:
            result = await asyncio.to_thread(
                execute_desktop_action,
                action,
                path,
                runner=deps.desktop_command_runner,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            safe_err = deps.config.redact_secrets(f"{type(exc).__name__}: {exc}")
            raise HTTPException(status_code=500, detail=safe_err) from exc

        return JSONResponse(result)

    return router
