from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from brain.artifact_store import ArtifactStore
from brain.config import AppConfig, load_config
from brain.context_store import ContextStore
from brain.history_store import MemoryStore
from brain.hub import SERVER_VERSION, Hub
from brain.intent_detector import ScreenIntentDetector
from brain.llm_router import LLMRouter
from brain.screen_router import ScreenUnderstandingRouter
from protocol.models import PROTOCOL_VERSION

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("qimingxing.main")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def create_app(
    config: Optional[AppConfig] = None,
    llm_router: Optional[LLMRouter] = None,
    context_store: Optional[ContextStore] = None,
    artifact_store: Optional[ArtifactStore] = None,
    memory_store: Optional[MemoryStore] = None,
    intent_detector: Optional[ScreenIntentDetector] = None,
    screen_router: Optional[ScreenUnderstandingRouter] = None,
    capture_timeout_seconds: float = 3.5,
) -> FastAPI:
    cfg = config or load_config()
    router = llm_router or LLMRouter(cfg)
    artifacts = artifact_store or ArtifactStore()
    hub = Hub(
        llm_router=router,
        config=cfg,
        context_store=context_store,
        artifact_store=artifacts,
        memory_store=memory_store,
        intent_detector=intent_detector,
        screen_router=screen_router,
        capture_timeout_seconds=capture_timeout_seconds,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def _ttl_reaper() -> None:
            while True:
                await asyncio.sleep(60)
                artifacts.cleanup_expired()

        reaper_task = asyncio.create_task(_ttl_reaper())
        try:
            yield
        finally:
            reaper_task.cancel()
            try:
                await reaper_task
            except asyncio.CancelledError:
                pass
            artifacts.cleanup_all()

    app = FastAPI(title="Qimingxing Brain", version=SERVER_VERSION, lifespan=lifespan)
    app.state.config = cfg
    app.state.llm_router = router
    app.state.artifact_store = artifacts
    app.state.hub = hub

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "server_version": SERVER_VERSION,
                "protocol_version": PROTOCOL_VERSION,
                "active_profile": router.active_profile,
                "privacy_mode": hub.privacy_mode,
            }
        )

    @app.post("/api/artifacts")
    async def upload_artifact(request: Request, file: UploadFile = File(...)) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(status_code=403, detail="Artifact uploads are restricted to localhost.")

        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="Empty artifact payload.")
        if len(data) > artifacts.max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Artifact exceeds maximum allowed size of {artifacts.max_bytes} bytes.",
            )

        rec = artifacts.save(data, content_type=file.content_type or "image/jpeg")
        return JSONResponse(
            {
                "artifact_id": rec.artifact_id,
                "expires_at": rec.expires_at,
                "size_bytes": rec.size_bytes,
            }
        )

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_REPO_ROOT / "body_web" / "index.html")

    @app.get("/app.js")
    async def body_js() -> FileResponse:
        return FileResponse(_REPO_ROOT / "body_web" / "app.js", media_type="application/javascript")

    @app.get("/styles.css")
    async def body_css() -> FileResponse:
        return FileResponse(_REPO_ROOT / "body_web" / "styles.css", media_type="text/css")

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        conn = hub.register(websocket)
        logger.info("WebSocket connected: client=%s", conn.client_id)
        try:
            while True:
                raw_text = await websocket.receive_text()
                await hub.handle_raw_message(conn, raw_text)
        except WebSocketDisconnect:
            pass
        finally:
            await hub.unregister(conn)

    return app


app = create_app()


def main() -> None:
    cfg = load_config()
    uvicorn.run(
        "brain.main:app",
        host=cfg.server.host,
        port=cfg.server.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
