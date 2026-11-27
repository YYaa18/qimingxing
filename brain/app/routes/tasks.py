from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from brain.app.routes._deps import RouteDeps
from brain.rich_content import collect_media


def build_tasks_router(*, deps: RouteDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/tasks")
    async def list_background_tasks(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in deps.loopback_hosts:
            raise HTTPException(
                status_code=403, detail="Task access is restricted to localhost."
            )
        status_filter = request.query_params.get("status") or None
        limit = max(1, min(100, int(request.query_params.get("limit") or 20)))
        tasks = await deps.task_runner.list_tasks(
            status=status_filter, limit=limit  # type: ignore[arg-type]
        )
        return JSONResponse(
            {
                "status": "ok",
                "active_count": deps.task_runner.active_count(),
                "tasks": [
                    {
                        "task_id": t.task_id,
                        "title": t.title,
                        "status": t.status,
                        "progress_pct": t.progress_pct,
                        "current_step": t.current_step,
                        "result_summary": t.result_summary,
                        "artifact_path": t.artifact_path,
                        "bundle_path": t.bundle_path,
                        "result_files": list(t.result_files),
                        "result_media": (
                            collect_media(t.result_files)
                            if t.status
                            in (
                                "completed",
                                "failed",
                                "cancelled",
                                "interrupted",
                                "stalled",
                            )
                            else []
                        ),
                        "error_message": t.error_message,
                        "created_at": t.created_at,
                        "started_at": t.started_at,
                        "finished_at": t.finished_at,
                    }
                    for t in tasks
                ],
            }
        )

    @router.post("/api/tasks/{task_id}/cancel")
    async def cancel_background_task(task_id: str, request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in deps.loopback_hosts:
            raise HTTPException(
                status_code=403, detail="Task control is restricted to localhost."
            )
        ok, msg = await deps.task_runner.cancel(task_id)
        if not ok and "未找到" in msg:
            raise HTTPException(status_code=404, detail=msg)
        return JSONResponse(
            {"status": "ok" if ok else "noop", "ok": ok, "message": msg}
        )

    return router
