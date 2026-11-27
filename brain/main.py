from __future__ import annotations

import asyncio
import html
import logging
import os
import shutil
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import uvicorn
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from brain.artifact_store import ArtifactStore
from brain.audio import ASRProvider, TTSProvider
from brain.config import (
    AppConfig,
    resolve_writable_config_path,
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
from brain.memory.threaded import ThreadedMemoryBackend
from brain.memory.embeddings import EmbeddingProvider, create_embedding_provider
from brain.memory.extractor import MemoryExtractor
from brain.memory.models import FeedbackRecord
from brain.model_config_store import (
    ModelConfigStore,
    ModelStoreConflictError,
    ModelStoreValidationError,
)
from brain.model_discovery import discover_provider_models
from brain.memory.retrieval import MemoryRetriever
from brain.rich_content import collect_media
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
    # Injected configs (tests/dev harnesses) keep their legacy config object.
    # The production Brain (no injected config) uses models.db as source of truth.
    enable_model_store = (not _config_injected) and (
        settings_config_path is not None or bool(os.environ.get("QIMINGXING_CONFIG"))
    )
    model_store: Optional[ModelConfigStore] = None
    model_store_mode = "disabled"
    if enable_model_store:
        cfg_path = Path(settings_config_path) if settings_config_path is not None else resolve_writable_config_path()
        candidate_store = ModelConfigStore(cfg_path.parent / "models.db", cfg_path)
        try:
            candidate_store.initialize(cfg)
            candidate_store.apply_to_config(cfg)
            model_store = candidate_store
            model_store_mode = "models_db"
        except Exception as exc:
            # Never brick startup because models.db migration/discovery is
            # unhealthy. Continue with the YAML-loaded cfg in fallback mode.
            logger.exception("models.db initialization failed; falling back to YAML: %s", exc)
            model_store = None
            model_store_mode = "yaml_fallback"
    router = llm_router or LLMRouter(cfg)
    artifacts = artifact_store or ArtifactStore()
    backend = memory_backend or create_memory_backend(cfg.memory)
    if memory_backend is None:
        # Production SQLite backend runs on its own loop thread so synchronous
        # queries never block streaming/TTS/approvals. Injected test backends
        # stay direct (tests often inspect them synchronously).
        backend = ThreadedMemoryBackend(backend)
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
    # Injected managers are test/harness objects: their startup must stay
    # synchronous so existing tests can assert connection state immediately.
    mcp_injected = mcp_manager is not None
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
        on_task_state_changed=hub.broadcast_task_state_changed,
        on_followup_acked=hub.broadcast_task_followup_acked,
        on_watchdog_alert=hub.broadcast_task_watchdog_alert,
    )
    hub.task_runner = task_runner

    mcp_state = {"starting": False}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await backend.initialize()
        await task_runner.initialize()
        await task_runner.start_watchdog()

        mcp_start_task: Optional[asyncio.Task[None]] = None

        async def _start_mcp_background() -> None:
            try:
                await active_mcp.start_all(hub.tool_registry)
            except Exception as exc:  # never let optional MCP kill readiness
                logger.warning(
                    "MCP background startup failed (tools stay unavailable): %s",
                    cfg.redact_secrets(f"{type(exc).__name__}: {exc}"),
                )

        if mcp_injected:
            # Preserve synchronous semantics for injected test/harness managers.
            await active_mcp.start_all(hub.tool_registry)
        else:
            # Optional capability: a slow/hanging MCP server must never delay
            # /health, the WS handshake or the first turn.
            mcp_state["starting"] = True
            mcp_start_task = asyncio.create_task(_start_mcp_background())
            mcp_start_task.add_done_callback(lambda _t: mcp_state.__setitem__("starting", False))

        await active_scheduler.start()

        warm_task: Optional[asyncio.Task[None]] = None
        if not _config_injected:
            # Move the first-turn TCP+TLS handshake off the critical path.
            warm_task = asyncio.create_task(router.warm_connection())

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
            if mcp_start_task is not None and not mcp_start_task.done():
                mcp_start_task.cancel()
                try:
                    await mcp_start_task
                except asyncio.CancelledError:
                    pass
            if warm_task is not None and not warm_task.done():
                warm_task.cancel()
                try:
                    await warm_task
                except asyncio.CancelledError:
                    pass
            await active_mcp.stop_all(hub.tool_registry)
            reaper_task.cancel()
            try:
                await reaper_task
            except asyncio.CancelledError:
                pass
            extractor.cancel_idle_timers()
            await extractor.wait_for_idle()
            await skill_manager.wait_for_idle()
            try:
                await router.aclose()
            except Exception:
                logger.debug("Failed closing shared LLM HTTP client", exc_info=True)
            try:
                await emb_provider.aclose()
            except Exception:
                logger.debug("Failed closing embedding HTTP client", exc_info=True)
            for provider in (hub.asr_provider, hub.tts_provider):
                close = getattr(provider, "aclose", None)
                if close is None:
                    continue
                try:
                    await close()
                except Exception:
                    logger.debug("Failed closing audio HTTP client", exc_info=True)
            screen_close = getattr(hub.screen_router, "aclose", None)
            if screen_close is not None:
                try:
                    await screen_close()
                except Exception:
                    logger.debug("Failed closing screen router HTTP client", exc_info=True)
            for tool in hub.tool_registry.list_tools():
                close = getattr(tool, "aclose", None)
                if close is None:
                    continue
                try:
                    await close()
                except Exception:
                    logger.debug("Failed closing tool HTTP client", exc_info=True)
            await backend.close()
            artifacts.cleanup_all()

    app = FastAPI(title="Qimingxing Brain", version=SERVER_VERSION, lifespan=lifespan)
    app.state.config = cfg
    app.state.llm_router = router
    app.state.artifact_store = artifacts
    app.state.model_store = model_store
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
                "model_store_mode": model_store_mode,
                "mcp_starting": mcp_state["starting"],
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
                "last_build_stats": (
                    asdict(hub.last_build_stats)
                    if getattr(hub, "last_build_stats", None) is not None
                    else None
                ),
                "today_usage": {
                    "calls": today_usage.calls,
                    "prompt_tokens": today_usage.prompt_tokens,
                    "cached_input_tokens": today_usage.cached_input_tokens,
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

    @app.post("/api/models/discover")
    async def discover_models(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(status_code=403, detail="Model discovery is restricted to localhost.")
        if model_store is None:
            raise HTTPException(status_code=503, detail="model_store_unavailable")
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc
        provider_id = str(body.get("provider_id") or "").strip()
        provider = model_store.provider(provider_id)
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
            safe = hub.config.redact_secrets(detail)
            if api_key and len(api_key) >= 4:
                safe = safe.replace(api_key, "***REDACTED***")
            raise HTTPException(status_code=502, detail=safe) from exc
        except Exception as exc:
            safe = hub.config.redact_secrets(f"{type(exc).__name__}: {exc}")
            raise HTTPException(status_code=502, detail=safe) from exc
        count = model_store.upsert_discovered(provider_id, discovered)
        model_store.apply_to_config(hub.config)
        await hub.apply_config_update(hub.config)
        return JSONResponse({
            "status": "ok",
            "count": count,
            "models": discovered,
        })

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
            if model_store is not None:
                if isinstance(body.get("providers"), dict):
                    model_store.apply_provider_patch(body["providers"])
                if isinstance(body.get("model_routing"), dict):
                    model_store.apply_routing_patch(body["model_routing"])
                body = {
                    key: value
                    for key, value in body.items()
                    if key not in ("providers", "model_routing", "profiles")
                }
            fresh_cfg = save_settings_to_yaml(body, config_path=settings_config_path)
            if model_store is not None:
                model_store.apply_to_config(fresh_cfg)
        except ModelStoreConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ModelStoreValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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

    @app.post("/api/feedback")
    async def submit_feedback(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
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

        backend = hub.memory_backend
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

    def _browser_data_dir() -> Path:
        return Path(cfg.memory.sqlite_path).expanduser().resolve().parent / "browser"

    def _browser_cdp_endpoint(raw: str = "") -> str:
        endpoint = (raw or os.environ.get("QMX_BROWSER_CDP") or "http://127.0.0.1:9222").strip().rstrip("/")
        parsed = urlparse(endpoint)
        if parsed.scheme != "http" or (parsed.hostname or "") not in {"127.0.0.1", "localhost", "::1"}:
            raise HTTPException(status_code=400, detail="CDP endpoint must be a local http://127.0.0.1 URL.")
        return endpoint

    @app.get("/api/browser/status")
    async def browser_status(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(status_code=403, detail="Browser status is restricted to localhost.")
        data_dir = _browser_data_dir()
        profile = data_dir / "browser_profile"
        size_bytes = 0
        if profile.exists():
            for item in profile.rglob("*"):
                try:
                    if item.is_file():
                        size_bytes += item.stat().st_size
                except OSError:
                    continue
        try:
            import playwright  # noqa: F401
            playwright_ready = True
        except Exception:
            playwright_ready = False
        endpoint = _browser_cdp_endpoint(
            str(getattr(getattr(cfg, "browser", None), "cdp_endpoint", "") or "")
        )
        cdp_ok = False
        cdp_error = ""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{endpoint}/json/version")
                cdp_ok = resp.status_code == 200
                if not cdp_ok:
                    cdp_error = f"HTTP {resp.status_code}"
        except Exception as exc:
            cdp_error = cfg.redact_secrets(str(exc))[:200]
        return JSONResponse(
            {
                "status": "ok",
                "chrome_installed": Path("/Applications/Google Chrome.app").exists(),
                "playwright_installed": playwright_ready,
                "profile_dir": str(profile),
                "profile_exists": profile.exists(),
                "profile_size_bytes": size_bytes,
                "cdp_endpoint": endpoint,
                "headless": bool(getattr(getattr(cfg, "browser", None), "headless", True)),
                "channel": str(getattr(getattr(cfg, "browser", None), "channel", "chrome")),
                "cdp_ok": cdp_ok,
                "cdp_error": cdp_error,
            }
        )

    @app.post("/api/browser/clear_profile")
    async def browser_clear_profile(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(status_code=403, detail="Browser profile cleanup is restricted to localhost.")
        profile = (_browser_data_dir() / "browser_profile").resolve()
        expected = (_browser_data_dir()).resolve() / "browser_profile"
        if profile != expected:
            raise HTTPException(status_code=400, detail="Refusing to delete an unexpected path.")
        freed = 0
        if profile.exists():
            for item in profile.rglob("*"):
                try:
                    if item.is_file():
                        freed += item.stat().st_size
                except OSError:
                    continue
            shutil.rmtree(profile)
        profile.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(profile, 0o700)
        except OSError:
            pass
        return JSONResponse({"status": "ok", "freed_bytes": freed, "profile_dir": str(profile)})

    @app.post("/api/browser/test_cdp")
    async def browser_test_cdp(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(status_code=403, detail="CDP probe is restricted to localhost.")
        try:
            body = await request.json()
        except Exception:
            body = {}
        endpoint = _browser_cdp_endpoint(str(body.get("endpoint") or "")) if isinstance(body, dict) else _browser_cdp_endpoint()
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                version = await client.get(f"{endpoint}/json/version")
                version.raise_for_status()
                tabs_resp = await client.get(f"{endpoint}/json/list")
                tabs_resp.raise_for_status()
            info = version.json()
            tabs = [
                {"title": t.get("title", ""), "url": t.get("url", "")}
                for t in tabs_resp.json()
                if isinstance(t, dict) and t.get("type") == "page"
            ]
            return JSONResponse(
                {
                    "status": "ok",
                    "ok": True,
                    "browser": info.get("Browser", ""),
                    "tabs": tabs[:10],
                }
            )
        except Exception as exc:
            return JSONResponse(
                {"status": "ok", "ok": False, "error": cfg.redact_secrets(str(exc))[:300]},
                status_code=200,
            )

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
                        "bundle_path": t.bundle_path,
                        "result_files": list(t.result_files),
                        "result_media": (
                            collect_media(t.result_files)
                            if t.status in ("completed", "failed", "cancelled", "interrupted", "stalled")
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
    async def index() -> JSONResponse:
        return JSONResponse(
            {
                "service": "qimingxing",
                "version": SERVER_VERSION,
                "status": "ok",
                "ui": "native",
            }
        )

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


_app: Optional[FastAPI] = None


def get_app() -> FastAPI:
    """Build the production app once, on first use.

    Keeping this out of module import means `import brain.main` (tests, CLI,
    tooling) no longer constructs the Hub, opens models.db or creates artifact
    temp dirs. `uvicorn brain.main:app` still works via module __getattr__.
    """
    global _app
    if _app is None:
        _app = create_app()
    return _app


def __getattr__(name: str) -> Any:
    if name == "app":
        return get_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main() -> None:
    import os

    app = get_app()
    cfg = app.state.config
    host = os.environ.get("QIMINGXING_HOST") or cfg.server.host
    port_env = os.environ.get("QIMINGXING_PORT")
    port = int(port_env) if port_env else cfg.server.port
    # Pass the object (not "brain.main:app") so `python -m brain.main` does not
    # import and execute this module a second time.
    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
