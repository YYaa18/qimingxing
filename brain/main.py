from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from brain.artifact_store import ArtifactStore
from brain.config import AppConfig, load_config
from brain.context_builder import ContextBuilder
from brain.context_store import ContextStore
from brain.history_store import MemoryStore
from brain.hub import SERVER_VERSION, Hub
from brain.intent_detector import ScreenIntentDetector
from brain.llm_router import LLMRouter
from brain.memory import MemoryBackend, create_memory_backend
from brain.memory.embeddings import EmbeddingProvider, create_embedding_provider
from brain.memory.extractor import MemoryExtractor
from brain.memory.retrieval import MemoryRetriever
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
    memory_backend: Optional[MemoryBackend] = None,
    embedding_provider: Optional[EmbeddingProvider] = None,
    intent_detector: Optional[ScreenIntentDetector] = None,
    screen_router: Optional[ScreenUnderstandingRouter] = None,
    capture_timeout_seconds: float = 3.5,
) -> FastAPI:
    cfg = config or load_config()
    router = llm_router or LLMRouter(cfg)
    artifacts = artifact_store or ArtifactStore()
    backend = memory_backend or create_memory_backend(cfg.memory)
    emb_provider = embedding_provider or create_embedding_provider(cfg.memory.embedding)
    retriever = MemoryRetriever(
        backend,
        emb_provider,
        timeout_ms=cfg.memory.embedding.timeout_ms,
    )
    context_builder = ContextBuilder(cfg.context_budget)
    extractor = MemoryExtractor(
        backend,
        router,
        emb_provider,
        cfg.memory.extraction,
        hot_message_limit=cfg.memory.hot_message_limit,
        archive_batch_size=cfg.memory.archive_batch_size,
    )
    from brain.memory.skills import SkillManager

    skill_manager = SkillManager(backend, router, cfg.tools.skills)

    hub = Hub(
        llm_router=router,
        config=cfg,
        context_store=context_store,
        artifact_store=artifacts,
        memory_store=memory_store,
        memory_backend=backend,
        memory_retriever=retriever,
        context_builder=context_builder,
        memory_extractor=extractor,
        intent_detector=intent_detector,
        screen_router=screen_router,
        skill_manager=skill_manager,
        capture_timeout_seconds=capture_timeout_seconds,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await backend.initialize()

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
            extractor.cancel_idle_timers()
            await extractor.wait_for_idle()
            await skill_manager.wait_for_idle()
            await backend.close()
            artifacts.cleanup_all()

    app = FastAPI(title="Qimingxing Brain", version=SERVER_VERSION, lifespan=lifespan)
    app.state.config = cfg
    app.state.llm_router = router
    app.state.artifact_store = artifacts
    app.state.memory_backend = backend
    app.state.memory_retriever = retriever
    app.state.memory_extractor = extractor
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
                "memory_backend": cfg.memory.backend,
                "embedding_enabled": emb_provider.enabled,
            }
        )

    @app.get("/api/memory/debug")
    async def memory_debug(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(
                status_code=403, detail="Memory debug inspection is restricted to localhost."
            )

        session = await backend.get_or_create_active_session()
        facts = await backend.list_facts(status="active", limit=50)
        episodes = await backend.list_episodes(status="active", limit=20)
        recent_msgs = await backend.get_messages(session.session_id, limit=12)
        categories = await backend.list_categories()
        hot_count, archived_count = await backend.count_messages()
        skills = await backend.list_skills(status="active", limit=30)
        work_logs = await backend.list_work_history(limit=10)
        import time as _time
        lt = _time.localtime()
        day_start = _time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        today_usage = await backend.get_usage_summary(since_ts=day_start)

        return JSONResponse(
            {
                "active_session": {
                    "session_id": session.session_id,
                    "status": session.status,
                    "rolling_summary": session.rolling_summary,
                    "last_summarized_msg_id": session.last_summarized_msg_id,
                    "recent_message_count": len(recent_msgs),
                },
                "hot_messages_count": hot_count,
                "archived_messages_count": archived_count,
                "skills_count": len(skills),
                "work_history_count": len(work_logs),
                "today_usage": {
                    "calls": today_usage.calls,
                    "prompt_tokens": today_usage.prompt_tokens,
                    "prompt_cache_hit_tokens": today_usage.prompt_cache_hit_tokens,
                    "completion_tokens": today_usage.completion_tokens,
                    "total_cost_cny": round(today_usage.total_cost_cny, 6),
                    "cache_hit_rate": round(today_usage.cache_hit_rate, 4),
                },
                "categories": [
                    {
                        "category_key": c.category_key,
                        "display_name": c.display_name,
                        "tier": c.tier,
                        "retrieval_weight": c.retrieval_weight,
                        "decay_rate": c.decay_rate,
                        "inject_to_l1": c.inject_to_l1,
                        "created_by": c.created_by,
                    }
                    for c in categories
                ],
                "active_facts_count": len(facts),
                "active_facts": [
                    {
                        "fact_id": f.fact_id,
                        "category": f.category,
                        "content": f.content,
                        "importance": f.importance,
                        "embedding_model": f.embedding_model,
                    }
                    for f in facts
                ],
                "recent_episodes": [
                    {
                        "episode_id": ep.episode_id,
                        "kind": ep.kind,
                        "summary": ep.summary,
                        "embedding_model": ep.embedding_model,
                    }
                    for ep in episodes
                ],
                "last_context_stats": asdict(hub.last_build_stats)
                if hub.last_build_stats
                else None,
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
    import os
    cfg = load_config()
    host = os.environ.get("QIMINGXING_HOST") or cfg.server.host
    port_env = os.environ.get("QIMINGXING_PORT")
    port = int(port_env) if port_env else cfg.server.port
    uvicorn.run(
        "brain.main:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
