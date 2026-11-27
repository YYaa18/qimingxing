from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from brain.app.routes._deps import RouteDeps
from brain.memory.models import FeedbackRecord


def build_feedback_router(*, deps: RouteDeps) -> APIRouter:
    router = APIRouter()

    @router.post("/api/feedback")
    async def submit_feedback(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in deps.loopback_hosts:
            raise HTTPException(
                status_code=403, detail="Feedback is restricted to localhost."
            )
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object.")
        rating = str(body.get("rating") or "").strip().lower()
        if rating not in ("up", "down"):
            raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'.")

        backend = deps.hub.memory_backend
        if backend is None:
            raise HTTPException(status_code=503, detail="memory_backend_unavailable")
        session_id = ""
        try:
            session = await backend.get_or_create_active_session()
            session_id = session.session_id
        except Exception:
            session_id = ""
        record = await backend.add_feedback(
            FeedbackRecord(
                feedback_id=0,
                turn_id=str(body.get("turn_id") or "")[:160],
                rating=rating,
                session_id=session_id,
                profile=str(body.get("profile") or "")[:120],
                prompt=str(body.get("prompt") or "")[:4000],
                answer=str(body.get("answer") or "")[:8000],
                source=str(body.get("source") or "shell_macos")[:80],
            )
        )
        return JSONResponse({"status": "ok", "feedback_id": record.feedback_id})

    return router
