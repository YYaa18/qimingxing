from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import uvicorn
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from brain.artifact_store import ArtifactStore
from brain.app.routes.artifacts import build_artifacts_router
from brain.app.routes.health import build_health_router
from brain.app.routes.browser import build_browser_router
from brain.app.routes.desktop import build_desktop_router
from brain.app.routes.feedback import build_feedback_router
from brain.app.routes.mcp import build_mcp_router
from brain.app.routes.memory import build_memory_router
from brain.app.routes.models import build_models_router
from brain.app.routes.settings import build_settings_router
from brain.app.routes.tasks import build_tasks_router
from brain.app.routes._deps import RouteDeps
from brain.audio import ASRProvider, TTSProvider
from brain.config import (
    AppConfig,
    load_config,
    resolve_writable_config_path,
)
from brain.context_builder import ContextBuilder
from brain.context_store import ContextStore
from brain.history_store import MemoryStore
from brain.hub import SERVER_VERSION, Hub
from brain.intent_detector import ScreenIntentDetector
from brain.llm_router import LLMRouter
from brain.memory import MemoryBackend, create_memory_backend
from brain.memory.threaded import ThreadedMemoryBackend
from brain.memory.embeddings import EmbeddingProvider, create_embedding_provider
from brain.memory.extractor import MemoryExtractor
from brain.model_config_store import ModelConfigStore
from brain.memory.retrieval import MemoryRetriever
from brain.scheduler import TaskScheduler
from brain.screen_router import DeepSeekOCRProvider, ScreenUnderstandingRouter
from brain.task_runner import BackgroundTaskRunner
from brain.tools.desktop_io import CommandRunner
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

    # -- Security gate ------------------------------------------------------
    # Loopback only, no CORS headers, no cross-site writes. Native clients and
    # the MCP OAuth callback send no Origin; every browser-originated state
    # change is rejected here instead of in each route.
    _allowed_host_names = {"127.0.0.1", "localhost", "::1", "testserver"}

    def _host_header_allowed(value: str) -> bool:
        host = (value or "").strip().lower()
        if not host:
            return False
        if host.startswith("["):
            host = host[1:].split("]", 1)[0]
        elif host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        return host in _allowed_host_names

    def _origin_allowed(value: str) -> bool:
        if not value:
            return True
        if value.strip().lower() == "null":
            return False
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return host in {"127.0.0.1", "localhost", "::1"} and port == int(cfg.server.port)

    @app.middleware("http")
    async def _security_gate(request: Request, call_next):
        if not _host_header_allowed(request.headers.get("host", "")):
            return JSONResponse({"detail": "invalid host"}, status_code=403)
        if not _origin_allowed(request.headers.get("origin", "")):
            return JSONResponse({"detail": "invalid origin"}, status_code=403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            raw_length = request.headers.get("content-length")
            has_body = request.headers.get("transfer-encoding") is not None or (
                raw_length is not None and raw_length.strip() not in ("", "0")
            )
            if has_body:
                content_type = (
                    request.headers.get("content-type", "").split(";")[0] or ""
                ).strip().lower()
                allowed = (
                    {"multipart/form-data"}
                    if request.url.path == "/api/artifacts"
                    else {"application/json"}
                )
                if content_type not in allowed:
                    return JSONResponse(
                        {"detail": "unsupported content type"}, status_code=415
                    )
        return await call_next(request)

    app.include_router(
        build_health_router(
            server_version=SERVER_VERSION,
            llm_router=router,
            hub=hub,
            config=cfg,
            embedding_provider=emb_provider,
            model_store_mode=model_store_mode,
            mcp_state=mcp_state,
        )
    )
    deps = RouteDeps(
        hub=hub,
        config=cfg,
        model_store=model_store,
        mcp=active_mcp,
        oauth=active_oauth,
        backend=backend,
        task_runner=task_runner,
        loopback_hosts=_LOOPBACK_HOSTS,
        settings_config_path=settings_config_path,
        config_injected=_config_injected,
        desktop_command_runner=desktop_command_runner,
        probe_transport=probe_transport,
    )
    app.include_router(build_memory_router(deps=deps))

    app.include_router(
        build_artifacts_router(artifacts=artifacts, loopback_hosts=_LOOPBACK_HOSTS)
    )

    app.include_router(build_mcp_router(deps=deps))

    app.include_router(build_desktop_router(deps=deps))

    app.include_router(build_models_router(deps=deps))

    app.include_router(build_settings_router(deps=deps))

    app.include_router(build_feedback_router(deps=deps))

    app.include_router(build_browser_router(deps=deps))

    app.include_router(build_tasks_router(deps=deps))

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
    # Loopback is a security boundary, not a preference: ignore non-loopback
    # config values and never let an env var override the bind address.
    host = cfg.server.host if cfg.server.host in {"127.0.0.1", "::1"} else "127.0.0.1"
    if host != cfg.server.host:
        logger.warning(
            "Ignoring non-loopback server.host=%r; binding to %s",
            cfg.server.host,
            host,
        )
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
