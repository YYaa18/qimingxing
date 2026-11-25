from __future__ import annotations

import asyncio
import html
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from brain.artifact_store import ArtifactStore
from brain.audio import ASRProvider, TTSProvider
from brain.config import (
    AppConfig,
    delete_mcp_server_in_yaml,
    get_sanitized_mcp_servers,
    get_sanitized_settings,
    load_config,
    save_settings_to_yaml,
    upsert_mcp_server_in_yaml,
)
from brain.context_builder import ContextBuilder
from brain.context_store import ContextStore
from brain.history_store import MemoryStore
from brain.hub import SERVER_VERSION, Hub
from brain.intent_detector import ScreenIntentDetector
from brain.llm_router import LLMRouter, probe_provider_connectivity
from brain.memory import MemoryBackend, create_memory_backend
from brain.memory.embeddings import EmbeddingProvider, create_embedding_provider
from brain.memory.extractor import MemoryExtractor
from brain.memory.retrieval import MemoryRetriever
from brain.scheduler import TaskScheduler
from brain.screen_router import DeepSeekOCRProvider, ScreenUnderstandingRouter
from brain.task_runner import BackgroundTaskRunner
from brain.tools.desktop_io import CommandRunner, execute_desktop_action
from brain.tools.mcp_client import MCPClientManager
from brain.tools.mcp_oauth import MCPOAuthCoordinator
from protocol.models import PROTOCOL_VERSION

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("qimingxing.main")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def is_allowed_ws_origin(origin: str, server_port: int = 18790) -> bool:
    """Allow empty Origin (native clients) or loopback Origin on allowed ports (defends against CSWSH)."""
    cleaned = (origin or "").strip()
    if not cleaned:
        return True
    from urllib.parse import urlparse

    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if host not in ("127.0.0.1", "localhost", "::1", "testserver"):
        return False
    if host == "testserver" and parsed.port is None:
        return True
    allowed_ports = {int(server_port), 18790}
    return parsed.port in allowed_ports


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
    mcp_manager: Optional[MCPClientManager] = None,
    oauth_coordinator: Optional[MCPOAuthCoordinator] = None,
    scheduler: Optional[TaskScheduler] = None,
    asr_provider: Optional[ASRProvider] = None,
    tts_provider: Optional[TTSProvider] = None,
    desktop_command_runner: Optional[CommandRunner] = None,
    settings_config_path: Optional[Path] = None,
    capture_timeout_seconds: float = 3.5,
    probe_transport: Optional[Any] = None,
) -> FastAPI:
    _config_injected = config is not None
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
        asr_provider=asr_provider,
        tts_provider=tts_provider,
        capture_timeout_seconds=capture_timeout_seconds,
    )
    active_oauth = oauth_coordinator or (
        mcp_manager.oauth_coordinator
        if mcp_manager is not None and mcp_manager.oauth_coordinator is not None
        else MCPOAuthCoordinator(
            callback_url=f"http://127.0.0.1:{cfg.server.port}/api/mcp/oauth/callback"
        )
    )
    if mcp_manager is not None:
        active_mcp = mcp_manager
        if active_mcp.oauth_coordinator is None:
            active_mcp.oauth_coordinator = active_oauth
    else:
        active_mcp = MCPClientManager(cfg, oauth_coordinator=active_oauth)
    active_scheduler = scheduler or TaskScheduler(
        backend,
        router,
        cfg,
        on_notice=hub.broadcast_proactive_notice,
        on_soft_alert=hub.broadcast_soft_alert,
    )
    task_runner = BackgroundTaskRunner(
        backend,
        router,
        cfg,
        hub.tool_registry,
        on_task_status=hub.broadcast_task_status,
        on_notice=hub.broadcast_proactive_notice,
        on_artifact_capsule=hub.broadcast_artifact_capsule,
        on_soft_alert=hub.broadcast_soft_alert,
        on_request_approval=hub.request_background_approval,
        on_ask_user=hub.request_background_clarification,
    )
    hub.task_runner = task_runner

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await backend.initialize()
        await task_runner.initialize()
        await active_mcp.start_all(hub.tool_registry)
        await active_scheduler.start()

        async def _ttl_reaper() -> None:
            while True:
                await asyncio.sleep(60)
                artifacts.cleanup_expired()

        reaper_task = asyncio.create_task(_ttl_reaper())
        try:
            yield
        finally:
            await task_runner.shutdown()
            await active_scheduler.stop()
            await active_mcp.stop_all(hub.tool_registry)
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
    app.state.scheduler = active_scheduler
    app.state.task_runner = task_runner
    app.state.mcp_manager = active_mcp
    app.state.oauth_coordinator = active_oauth

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
        scheduled_tasks = await backend.list_scheduled_tasks(status="active", limit=100)
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
                "scheduled_tasks_count": len(scheduled_tasks),
                "mcp_servers": [asdict(s) for s in active_mcp.list_statuses()],
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

        try:
            rec = artifacts.save(
                data,
                content_type=file.content_type or "application/octet-stream",
                filename=file.filename or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(
            {
                "artifact_id": rec.artifact_id,
                "kind": rec.kind,
                "filename": rec.filename,
                "content_type": rec.content_type,
                "expires_at": rec.expires_at,
                "size_bytes": rec.size_bytes,
            }
        )

    @app.post("/api/mcp/reload")
    async def reload_mcp_servers(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(status_code=403, detail="MCP reload is restricted to localhost.")

        new_servers = None
        if not _config_injected:
            try:
                fresh_cfg = load_config()
                new_servers = fresh_cfg.mcp_servers
            except Exception as exc:
                safe_err = cfg.redact_secrets(f"{type(exc).__name__}: {exc}")
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to load config for MCP reload: {safe_err}",
                ) from exc

        statuses = await active_mcp.reload(hub.tool_registry, servers=new_servers)
        return JSONResponse(
            {
                "status": "ok",
                "mcp_servers": [asdict(s) for s in statuses],
            }
        )

    def _merged_mcp_server_views() -> List[Dict[str, Any]]:
        status_map = {s.name: asdict(s) for s in active_mcp.list_statuses()}
        out: List[Dict[str, Any]] = []
        for item in get_sanitized_mcp_servers(hub.config):
            st = status_map.get(item["name"], {})
            merged = dict(item)
            merged["status"] = st.get("status", "disabled" if not item["enabled"] else "offline")
            merged["tools"] = st.get("tools", [])
            merged["error"] = st.get("error", "")
            merged["has_oauth_token"] = bool(
                st.get("has_oauth_token", active_oauth.has_saved_token(item["name"]))
            )
            out.append(merged)
        return out

    @app.get("/api/mcp/servers")
    async def list_mcp_servers(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(status_code=403, detail="MCP config access is restricted to localhost.")
        return JSONResponse({"status": "ok", "servers": _merged_mcp_server_views()})

    @app.post("/api/mcp/servers")
    async def upsert_mcp_server(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(status_code=403, detail="MCP config updates are restricted to localhost.")
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object.")

        try:
            fresh_cfg = upsert_mcp_server_in_yaml(body, config_path=settings_config_path)
        except Exception as exc:
            safe_err = hub.config.redact_secrets(f"{type(exc).__name__}: {exc}")
            raise HTTPException(status_code=400, detail=f"保存 MCP 配置失败: {safe_err}") from exc

        hub.config = fresh_cfg
        await active_mcp.reload(hub.tool_registry, servers=fresh_cfg.mcp_servers)
        return JSONResponse({"status": "ok", "servers": _merged_mcp_server_views()})

    @app.delete("/api/mcp/servers/{server_name}")
    async def remove_mcp_server(server_name: str, request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(status_code=403, detail="MCP deletion is restricted to localhost.")
        clear_auth = str(request.query_params.get("clear_auth", "true")).lower() != "false"
        fresh_cfg = delete_mcp_server_in_yaml(server_name, config_path=settings_config_path)
        if clear_auth:
            active_oauth.clear_saved_auth(server_name)
        hub.config = fresh_cfg
        await active_mcp.reload(hub.tool_registry, servers=fresh_cfg.mcp_servers)
        return JSONResponse({"status": "ok", "servers": _merged_mcp_server_views()})

    @app.post("/api/mcp/servers/{server_name}/authorize")
    async def authorize_mcp_server(server_name: str, request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(status_code=403, detail="MCP authorization is restricted to localhost.")
        result = await active_mcp.start_oauth_authorization(server_name, hub.tool_registry)
        return JSONResponse(
            {
                **result,
                "servers": _merged_mcp_server_views(),
            }
        )

    @app.get("/api/mcp/oauth/callback")
    async def mcp_oauth_callback(
        code: Optional[str] = None,
        state: Optional[str] = None,
        iss: Optional[str] = None,
        error: Optional[str] = None,
        error_description: Optional[str] = None,
    ) -> HTMLResponse:
        ok, message = active_oauth.complete_callback(
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

    @app.post("/api/context/attach_paths")
    async def attach_paths(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
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

        res = hub.attach_context_paths(
            [str(p) for p in raw_paths], source=source, append=append
        )
        if res.accepted_paths:
            await hub.broadcast_tool_notice(
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
                "pending_roots": hub.batch_path_store.pending_count(),
                "accepted_paths": res.accepted_paths,
                "filtered_paths": res.filtered_paths,
                "manifest_preview": res.manifest_text,
            }
        )

    @app.post("/api/desktop/open_action")
    async def desktop_open_action(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(
                status_code=403, detail="Desktop open actions are restricted to localhost."
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
                execute_desktop_action, action, path, runner=desktop_command_runner
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            safe_err = cfg.redact_secrets(f"{type(exc).__name__}: {exc}")
            raise HTTPException(status_code=500, detail=safe_err) from exc

        return JSONResponse(result)

    @app.get("/api/config/settings")
    async def get_settings(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(
                status_code=403, detail="Settings access is restricted to localhost."
            )
        return JSONResponse(get_sanitized_settings(hub.config))

    @app.post("/api/config/settings")
    async def update_settings(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
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
            fresh_cfg = save_settings_to_yaml(body, config_path=settings_config_path)
        except Exception as exc:
            safe_err = hub.config.redact_secrets(f"{type(exc).__name__}: {exc}")
            raise HTTPException(status_code=400, detail=f"保存配置失败: {safe_err}") from exc

        await hub.apply_config_update(fresh_cfg)
        return JSONResponse(
            {
                "status": "ok",
                "settings": get_sanitized_settings(fresh_cfg),
            }
        )

    @app.post("/api/config/test_provider")
    async def test_provider(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(
                status_code=403, detail="Provider connectivity probes are restricted to localhost."
            )
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object.")

        res = await probe_provider_connectivity(
            hub.config,
            provider_id=str(body.get("provider_id") or body.get("name") or "").strip(),
            base_url=str(body.get("base_url") or "").strip(),
            api_key=str(body.get("api_key") or "").strip(),
            protocol=str(body.get("protocol") or "openai_compatible").strip(),
            model=str(body.get("model") or "").strip(),
            transport=probe_transport,
        )
        return JSONResponse(res)

    @app.get("/api/tasks")
    async def list_background_tasks(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(status_code=403, detail="Task access is restricted to localhost.")
        status_filter = request.query_params.get("status") or None
        limit = max(1, min(100, int(request.query_params.get("limit") or 20)))
        tasks = await task_runner.list_tasks(status=status_filter, limit=limit)  # type: ignore[arg-type]
        return JSONResponse(
            {
                "status": "ok",
                "active_count": task_runner.active_count(),
                "tasks": [
                    {
                        "task_id": t.task_id,
                        "title": t.title,
                        "status": t.status,
                        "progress_pct": t.progress_pct,
                        "current_step": t.current_step,
                        "result_summary": t.result_summary,
                        "artifact_path": t.artifact_path,
                        "error_message": t.error_message,
                        "created_at": t.created_at,
                        "started_at": t.started_at,
                        "finished_at": t.finished_at,
                    }
                    for t in tasks
                ],
            }
        )

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_background_task(task_id: str, request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(status_code=403, detail="Task control is restricted to localhost.")
        ok, msg = await task_runner.cancel(task_id)
        if not ok and "未找到" in msg:
            raise HTTPException(status_code=404, detail=msg)
        return JSONResponse({"status": "ok" if ok else "noop", "ok": ok, "message": msg})

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
        origin = websocket.headers.get("origin", "")
        if not is_allowed_ws_origin(origin, server_port=cfg.server.port):
            logger.warning("Rejected WebSocket connection with disallowed Origin: %r", origin)
            await websocket.close(code=1008)
            return
        await websocket.accept()
        conn = hub.register(websocket)
        logger.info("WebSocket connected: client=%s", conn.client_id)
        try:
            while True:
                msg = await websocket.receive()
                msg_type = msg.get("type")
                if msg_type == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    await hub.handle_raw_message(conn, msg["text"])
                elif msg.get("bytes") is not None:
                    await hub.handle_raw_bytes(conn, msg["bytes"])
        except WebSocketDisconnect:
            pass
        finally:
            await hub.unregister(conn, websocket=websocket)

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
