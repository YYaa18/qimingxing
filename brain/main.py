from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from brain.config import AppConfig, load_config
from brain.context_store import ContextStore
from brain.hub import SERVER_VERSION, Hub
from brain.llm_router import LLMRouter
from protocol.models import PROTOCOL_VERSION

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("qimingxing.main")

_REPO_ROOT = Path(__file__).resolve().parent.parent


def create_app(
    config: Optional[AppConfig] = None,
    llm_router: Optional[LLMRouter] = None,
    context_store: Optional[ContextStore] = None,
) -> FastAPI:
    cfg = config or load_config()
    router = llm_router or LLMRouter(cfg)
    hub = Hub(llm_router=router, context_store=context_store)

    app = FastAPI(title="Qimingxing Brain", version=SERVER_VERSION)
    app.state.config = cfg
    app.state.llm_router = router
    app.state.hub = hub

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "server_version": SERVER_VERSION,
                "protocol_version": PROTOCOL_VERSION,
                "active_profile": router.active_profile,
            }
        )

    @app.get("/")
    async def index() -> FileResponse:
        html_path = _REPO_ROOT / "body_web" / "index.html"
        return FileResponse(html_path)

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
