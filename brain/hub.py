from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fastapi import WebSocket
from pydantic import ValidationError

from brain.artifact_store import ArtifactStore
from brain.config import AppConfig
from brain.context_builder import BuildStats, ContextBuilder
from brain.context_store import ContextStore
from brain.history_store import InMemorySlidingWindowStore, MemoryStore
from brain.intent_detector import ScreenIntentDetector
from brain.llm_router import LLMError, LLMRouter
from brain.memory.backend import MemoryBackend
from brain.memory.embeddings import NullEmbeddingProvider
from brain.memory.extractor import MemoryExtractor
from brain.memory.models import MemoryFact
from brain.memory.retrieval import MemoryRetriever
from brain.screen_router import DeepSeekOCRProvider, ScreenUnderstandingRouter
from brain.stream_parser import CardEvent, EmotionEvent, StreamParser, TextEvent
from protocol.models import (
    AvatarStatePayload,
    CardAction,
    ClientCapabilities,
    ContextGameStatePayload,
    ContextOcrSnippetPayload,
    ContextScreenRefPayload,
    ContextWindowChangedPayload,
    Envelope,
    InputActionPayload,
    InputCancelPayload,
    InputTextPayload,
    OutputCardPayload,
    OutputTextDeltaPayload,
    OutputToolStatusPayload,
    OutputTurnEndPayload,
    OutputTurnStartPayload,
    PermissionStatus,
    PrivacyMode,
    SensesCaptureRequestPayload,
    SourceRole,
    SystemConfigSetPayload,
    SystemErrorPayload,
    SystemHelloPayload,
    SystemPingPayload,
    SystemPongPayload,
    SystemPrivacySetPayload,
    SystemPrivacyStatePayload,
    SystemWelcomePayload,
    make_envelope,
    new_turn_id,
    parse_envelope,
    parse_payload,
)

logger = logging.getLogger("qimingxing.hub")

SERVER_VERSION = "0.3.0-mvp3"
CAPTURE_TIMEOUT_SECONDS = 3.5


@dataclass
class ClientConnection:
    client_id: str
    websocket: WebSocket
    role: Optional[SourceRole] = None
    client_name: str = "unknown"
    capabilities: ClientCapabilities = field(default_factory=ClientCapabilities)
    active_turn_id: Optional[str] = None
    active_task: Optional[asyncio.Task[None]] = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send_envelope(self, envelope: Envelope) -> None:
        async with self.send_lock:
            await self.websocket.send_text(envelope.model_dump_json())


class Hub:
    """Central star-topology WebSocket hub orchestrating Body, Senses, and Brain components."""

    def __init__(
        self,
        llm_router: LLMRouter,
        *,
        config: Optional[AppConfig] = None,
        context_store: Optional[ContextStore] = None,
        artifact_store: Optional[ArtifactStore] = None,
        memory_store: Optional[MemoryStore] = None,
        memory_backend: Optional[MemoryBackend] = None,
        memory_retriever: Optional[MemoryRetriever] = None,
        context_builder: Optional[ContextBuilder] = None,
        memory_extractor: Optional[MemoryExtractor] = None,
        intent_detector: Optional[ScreenIntentDetector] = None,
        screen_router: Optional[ScreenUnderstandingRouter] = None,
        capture_timeout_seconds: float = CAPTURE_TIMEOUT_SECONDS,
    ) -> None:
        self.llm_router = llm_router
        self.config = config or AppConfig()
        self.context_store = context_store or ContextStore()
        self.artifact_store = artifact_store or ArtifactStore()
        self.memory_store: MemoryStore = memory_store or InMemorySlidingWindowStore()
        self.memory_backend: Optional[MemoryBackend] = memory_backend
        self.context_builder = context_builder or ContextBuilder(self.config.context_budget)
        if memory_retriever is not None:
            self.memory_retriever: Optional[MemoryRetriever] = memory_retriever
        elif memory_backend is not None:
            self.memory_retriever = MemoryRetriever(
                memory_backend,
                NullEmbeddingProvider(),
                timeout_ms=self.config.memory.embedding.timeout_ms,
            )
        else:
            self.memory_retriever = None
        if memory_extractor is not None:
            self.memory_extractor: Optional[MemoryExtractor] = memory_extractor
        elif memory_backend is not None:
            self.memory_extractor = MemoryExtractor(
                memory_backend,
                llm_router,
                NullEmbeddingProvider(),
                self.config.memory.extraction,
                hot_message_limit=self.config.memory.hot_message_limit,
                archive_batch_size=self.config.memory.archive_batch_size,
            )
        else:
            self.memory_extractor = None

        self.intent_detector = intent_detector or ScreenIntentDetector()
        self.screen_router = screen_router or ScreenUnderstandingRouter(
            DeepSeekOCRProvider(self.config.ocr, self.config)
        )
        self.capture_timeout_seconds = capture_timeout_seconds

        self.clients: Dict[str, ClientConnection] = {}
        self._conn_seq = 0
        self.privacy_mode: PrivacyMode = self.config.privacy.default_mode
        self.senses_platform: str = "unknown"
        self.senses_permissions = PermissionStatus()
        self.senses_permission_hint: str = ""
        self._pending_captures: Dict[str, asyncio.Future[ContextScreenRefPayload]] = {}
        self.last_build_stats: Optional[BuildStats] = None

    def register(self, websocket: WebSocket) -> ClientConnection:
        self._conn_seq += 1
        client_id = f"client_{self._conn_seq}"
        conn = ClientConnection(client_id=client_id, websocket=websocket)
        self.clients[client_id] = conn
        return conn

    async def unregister(self, conn: ClientConnection) -> None:
        await self._cancel_active_turn(conn, notify=False)
        self.clients.pop(conn.client_id, None)
        logger.info("Disconnected client=%s role=%s", conn.client_id, conn.role)
        if conn.role == "senses":
            await self._broadcast_privacy_state()

    def _get_active_senses(self) -> Optional[ClientConnection]:
        for client in self.clients.values():
            if client.role == "senses":
                return client
        return None

    async def handle_raw_message(self, conn: ClientConnection, raw_text: str) -> None:
        try:
            envelope = parse_envelope(raw_text)
        except (ValueError, ValidationError) as exc:
            logger.warning("Invalid envelope from %s: %s", conn.client_id, exc)
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "system.error",
                    SystemErrorPayload(
                        code="INVALID_ENVELOPE",
                        message="消息信封格式非法，已忽略。",
                        recoverable=True,
                    ),
                )
            )
            return

        try:
            payload = parse_payload(envelope)
        except ValidationError as exc:
            logger.warning("Invalid payload for %s from %s: %s", envelope.type, conn.client_id, exc)
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "system.error",
                    SystemErrorPayload(
                        code="INVALID_PAYLOAD",
                        message=f"事件 {envelope.type} 的 payload 字段校验失败。",
                        recoverable=True,
                    ),
                    turn_id=envelope.turn_id,
                )
            )
            return

        if payload is None:
            logger.debug("Ignoring unknown event type=%s from client=%s", envelope.type, conn.client_id)
            return

        await self._dispatch(conn, envelope, payload)

    async def _dispatch(self, conn: ClientConnection, env: Envelope, payload: object) -> None:
        if isinstance(payload, SystemHelloPayload):
            conn.role = payload.role
            conn.client_name = payload.client_name
            conn.capabilities = payload.capabilities
            if payload.role == "senses":
                raw_payload = env.payload or {}
                self.senses_platform = str(raw_payload.get("platform", self.senses_platform))
                perms = raw_payload.get("permissions")
                if isinstance(perms, dict):
                    self.senses_permissions = PermissionStatus.model_validate(perms)
                self.senses_permission_hint = str(raw_payload.get("permission_hint", ""))
            logger.info(
                "Handshake hello: client=%s role=%s name=%s avatar_mode=%s",
                conn.client_id,
                conn.role,
                conn.client_name,
                conn.capabilities.avatar_mode,
            )
            await self._send_welcome(conn)
            await self._send_privacy_state(conn)
            if payload.role == "senses":
                await self._broadcast_privacy_state(exclude_client_id=conn.client_id)
            return

        if isinstance(payload, SystemPingPayload):
            await conn.send_envelope(
                make_envelope("brain", "system.pong", SystemPongPayload(client_ts=payload.client_ts))
            )
            return

        if isinstance(payload, SystemConfigSetPayload):
            try:
                self.llm_router.set_active_profile(payload.profile)
            except LLMError as exc:
                await conn.send_envelope(
                    make_envelope(
                        "brain",
                        "system.error",
                        SystemErrorPayload(code=exc.code, message=exc.message, recoverable=exc.recoverable),
                    )
                )
                return
            await self._broadcast_welcome_to_bodies()
            return

        if isinstance(payload, SystemPrivacySetPayload):
            self.privacy_mode = payload.mode
            if self.privacy_mode == "off":
                self.context_store.clear_window_context()
            logger.info("Privacy mode updated -> %s", self.privacy_mode)
            await self._broadcast_privacy_state()
            return

        if isinstance(payload, ContextWindowChangedPayload):
            if self.privacy_mode == "off":
                logger.debug("Ignoring window update because privacy_mode is OFF")
                return
            self.context_store.update_window(payload)
            logger.info(
                "Updated context.window_changed: app=%s masked=%s self=%s",
                payload.app_name,
                payload.privacy_masked,
                payload.is_self_window,
            )
            await self._broadcast_privacy_state()
            return

        if isinstance(payload, ContextGameStatePayload):
            self.context_store.update_game_state(payload)
            return

        if isinstance(payload, ContextOcrSnippetPayload):
            self.context_store.update_ocr(payload)
            return

        if isinstance(payload, ContextScreenRefPayload):
            fut = self._pending_captures.get(payload.request_id)
            if fut is not None and not fut.done():
                fut.set_result(payload)
            elif payload.artifact_id:
                self.artifact_store.delete(payload.artifact_id)
            return

        if isinstance(payload, InputCancelPayload):
            target_turn = env.turn_id or conn.active_turn_id
            logger.info(
                "Received input.cancel from %s target_turn=%s reason=%s",
                conn.client_id,
                target_turn,
                payload.reason,
            )
            await self._cancel_active_turn(conn, notify=True, target_turn_id=target_turn)
            return

        if isinstance(payload, InputTextPayload):
            turn_id = env.turn_id or new_turn_id()
            await self._start_turn(conn, turn_id, payload.text, force_screen=False)
            return

        if isinstance(payload, InputActionPayload):
            await self._handle_action(conn, env, payload)
            return

    async def _handle_action(
        self,
        conn: ClientConnection,
        env: Envelope,
        payload: InputActionPayload,
    ) -> None:
        if payload.action == "clear_history":
            self.memory_store.clear(conn.client_id)
            if self.memory_backend is not None:
                prev_session = await self.memory_backend.get_or_create_active_session()
                await self.memory_backend.create_new_session(archive_previous=True)
                if self.memory_extractor is not None:
                    self.memory_extractor.schedule_session_archive(
                        session_id=prev_session.session_id,
                        context_store=self.context_store,
                    )
            logger.info("Cleared short-term memory & archived active session for client=%s", conn.client_id)
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "output.tool_status",
                    OutputToolStatusPayload(
                        status="completed",
                        tool="memory",
                        label="已清空本轮对话并归档记忆（桌面感知状态保持正常）",
                    ),
                )
            )
            return

        turn_id = env.turn_id or new_turn_id()
        if payload.action == "ask_with_screen":
            user_text = str(payload.data.get("text") or "").strip() or "帮我看看我现在正在看的屏幕内容。"
            await self._start_turn(conn, turn_id, user_text, force_screen=True)
            return

        if payload.action == "more_hint":
            prompt_text = "请基于刚才的防剧透提示，给出更进一步的第 2 级提示（依然尽量避免直接剧透结局）。"
        else:
            prompt_text = f"用户触发快捷动作 `{payload.action}`（参数: {payload.data}），请简短响应。"
        await self._start_turn(conn, turn_id, prompt_text, force_screen=False)

    async def _send_welcome(self, conn: ClientConnection) -> None:
        welcome = SystemWelcomePayload(
            server_version=SERVER_VERSION,
            active_profile=self.llm_router.active_profile,
            profiles=self.llm_router.list_public_profiles(),
        )
        await conn.send_envelope(make_envelope("brain", "system.welcome", welcome))

    def _build_privacy_state(self) -> SystemPrivacyStatePayload:
        return SystemPrivacyStatePayload(
            mode=self.privacy_mode,
            senses_connected=self._get_active_senses() is not None,
            platform=self.senses_platform,
            permissions=self.senses_permissions,
            permission_hint=self.senses_permission_hint,
            active_window_summary=self.context_store.get_window_summary(),
        )

    async def _send_privacy_state(self, conn: ClientConnection) -> None:
        state_payload = self._build_privacy_state()
        await conn.send_envelope(make_envelope("brain", "system.privacy_state", state_payload))

    async def _broadcast_privacy_state(self, *, exclude_client_id: Optional[str] = None) -> None:
        state_payload = self._build_privacy_state()
        for client in list(self.clients.values()):
            if exclude_client_id and client.client_id == exclude_client_id:
                continue
            if client.role in ("body", "senses"):
                try:
                    await client.send_envelope(
                        make_envelope("brain", "system.privacy_state", state_payload)
                    )
                except Exception:
                    logger.debug("Failed sending privacy_state to %s", client.client_id)

    async def _broadcast_welcome_to_bodies(self) -> None:
        for client in list(self.clients.values()):
            if client.role in ("body", None):
                try:
                    await self._send_welcome(client)
                except Exception:
                    logger.debug("Failed sending welcome broadcast to %s", client.client_id)

    async def _cancel_active_turn(
        self,
        conn: ClientConnection,
        *,
        notify: bool,
        target_turn_id: Optional[str] = None,
    ) -> None:
        task = conn.active_task
        old_turn_id = conn.active_turn_id
        if task is None or task.done():
            conn.active_task = None
            conn.active_turn_id = None
            return

        if target_turn_id and old_turn_id and target_turn_id != old_turn_id:
            return

        logger.info("Cancelling active turn task turn_id=%s", old_turn_id)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            if conn.active_task is task:
                conn.active_task = None
                conn.active_turn_id = None

        if notify and old_turn_id:
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "output.turn_end",
                    OutputTurnEndPayload(finish_reason="cancelled"),
                    turn_id=old_turn_id,
                )
            )
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "avatar.state",
                    AvatarStatePayload(emotion="neutral", intensity=0.5, activity="idle"),
                    turn_id=old_turn_id,
                )
            )

    async def _start_turn(
        self,
        conn: ClientConnection,
        turn_id: str,
        user_text: str,
        *,
        force_screen: bool,
    ) -> None:
        await self._cancel_active_turn(conn, notify=True)
        conn.active_turn_id = turn_id
        task = asyncio.create_task(
            self._run_turn(conn, turn_id, user_text, force_screen=force_screen)
        )
        conn.active_task = task

    async def _request_screen_capture(
        self,
        conn: ClientConnection,
        turn_id: str,
        reason: str,
    ) -> tuple[Optional[ContextScreenRefPayload], Optional[str]]:
        senses_conn = self._get_active_senses()
        if senses_conn is None:
            return None, "当前桌面感官服务尚未连接，暂时看不到你的屏幕，请先启动感官客户端（python -m senses.main）。"

        req_id = f"cap_{uuid.uuid4().hex[:10]}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ContextScreenRefPayload] = loop.create_future()
        self._pending_captures[req_id] = fut

        await conn.send_envelope(
            make_envelope(
                "brain",
                "output.tool_status",
                OutputToolStatusPayload(
                    status="running",
                    tool="screen_capture",
                    label="正在查看当前桌面…",
                ),
                turn_id=turn_id,
            )
        )

        try:
            await senses_conn.send_envelope(
                make_envelope(
                    "brain",
                    "senses.capture_request",
                    SensesCaptureRequestPayload(
                        request_id=req_id,
                        turn_id=turn_id,
                        reason=reason,  # type: ignore[arg-type]
                        max_edge=1280,
                    ),
                    turn_id=turn_id,
                )
            )
            screen_ref = await asyncio.wait_for(fut, timeout=self.capture_timeout_seconds)
            return screen_ref, None
        except asyncio.TimeoutError:
            logger.warning("Screen capture timed out after %.1fs (turn=%s)", self.capture_timeout_seconds, turn_id)
            return None, "获取桌面画面超时了（超过 3.5 秒），暂时没能看到你的屏幕，你可以稍后再试一次。"
        finally:
            self._pending_captures.pop(req_id, None)

    async def _emit_direct_guidance(
        self,
        conn: ClientConnection,
        turn_id: str,
        message: str,
        *,
        emotion: str = "warning",
    ) -> None:
        await conn.send_envelope(
            make_envelope(
                "brain",
                "avatar.state",
                AvatarStatePayload(emotion=emotion, intensity=0.7, activity="speaking"),  # type: ignore[arg-type]
                turn_id=turn_id,
            )
        )
        await conn.send_envelope(
            make_envelope(
                "brain",
                "output.text_delta",
                OutputTextDeltaPayload(seq=1, delta=message, is_final=False),
                turn_id=turn_id,
            )
        )
        await conn.send_envelope(
            make_envelope(
                "brain",
                "output.text_delta",
                OutputTextDeltaPayload(seq=2, delta="", is_final=True),
                turn_id=turn_id,
            )
        )
        await conn.send_envelope(
            make_envelope(
                "brain",
                "output.turn_end",
                OutputTurnEndPayload(finish_reason="completed", elapsed_ms=10),
                turn_id=turn_id,
            )
        )
        await conn.send_envelope(
            make_envelope(
                "brain",
                "avatar.state",
                AvatarStatePayload(emotion="neutral", intensity=0.5, activity="idle"),
                turn_id=turn_id,
            )
        )

    async def _run_turn(
        self,
        conn: ClientConnection,
        turn_id: str,
        user_text: str,
        *,
        force_screen: bool = False,
    ) -> None:
        started = time.perf_counter()
        profile = self.llm_router.get_profile()
        parser = StreamParser()
        seq = 0
        collected_spoken: List[str] = []
        artifact_id_to_burn = ""

        try:
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "output.turn_start",
                    OutputTurnStartPayload(
                        profile=profile.name,
                        provider=profile.provider,
                        model=profile.model,
                    ),
                    turn_id=turn_id,
                )
            )
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "avatar.state",
                    AvatarStatePayload(emotion="thinking", intensity=0.7, activity="thinking"),
                    turn_id=turn_id,
                )
            )

            wants_screen = self.intent_detector.detect(user_text, explicit_action=force_screen)
            user_content: Any = user_text

            if wants_screen:
                if self.privacy_mode != "full_on_demand":
                    mode_label = "「关闭」" if self.privacy_mode == "off" else "「仅窗口信息」"
                    await self._emit_direct_guidance(
                        conn,
                        turn_id,
                        f"当前感知开关处于{mode_label}模式，我还没拿到看屏授权呢。点击顶部切换到「按需看屏」后再问我吧！",
                        emotion="confused",
                    )
                    return

                reason = "user_action" if force_screen else "intent_triggered"
                screen_ref, degrade_msg = await self._request_screen_capture(conn, turn_id, reason)
                if degrade_msg is not None or screen_ref is None:
                    await self._emit_direct_guidance(
                        conn,
                        turn_id,
                        degrade_msg or "暂时无法获取屏幕画面。",
                        emotion="confused",
                    )
                    return

                if screen_ref.artifact_id:
                    artifact_id_to_burn = screen_ref.artifact_id

                if screen_ref.status == "privacy_blocked":
                    await self._emit_direct_guidance(
                        conn,
                        turn_id,
                        "你当前正处于敏感隐私窗口（黑名单已自动拦截），为了保护隐私这次我不能看屏幕哦。",
                        emotion="warning",
                    )
                    return
                if screen_ref.status == "permission_denied":
                    hint = screen_ref.error_message or self.senses_permission_hint or "请检查系统屏幕录制权限。"
                    await self._emit_direct_guidance(
                        conn,
                        turn_id,
                        f"系统屏幕录制权限还没打开，我看不到画面：{hint}",
                        emotion="warning",
                    )
                    return
                if screen_ref.status != "ok" or not screen_ref.artifact_id:
                    await self._emit_direct_guidance(
                        conn,
                        turn_id,
                        screen_ref.error_message or "这次截屏遇到了一点小问题，没能拿到画面。",
                        emotion="confused",
                    )
                    return

                loaded = self.artifact_store.read_bytes(screen_ref.artifact_id)
                if loaded is None:
                    await self._emit_direct_guidance(
                        conn,
                        turn_id,
                        "临时截图文件已失效或被清理，请再试一次。",
                        emotion="confused",
                    )
                    return

                img_bytes, content_type = loaded
                if not profile.supports_vision:
                    await conn.send_envelope(
                        make_envelope(
                            "brain",
                            "output.tool_status",
                            OutputToolStatusPayload(
                                status="running",
                                tool="deepseek_ocr",
                                label="正在用云端 OCR 读取画面文字…",
                            ),
                            turn_id=turn_id,
                        )
                    )
                user_content, _ = await self.screen_router.prepare_user_message(
                    user_text=user_text,
                    image_bytes=img_bytes,
                    content_type=content_type,
                    profile=profile,
                    screen_ref=screen_ref,
                )

            active_session_id = ""
            rolling_summary = ""
            core_facts: List[MemoryFact] = []
            recalled_memories = []
            retrieval_ms = 0.0

            if self.memory_backend is not None:
                session = await self.memory_backend.get_or_create_active_session()
                active_session_id = session.session_id
                rolling_summary = session.rolling_summary
                raw_msgs = await self.memory_backend.get_messages(
                    session.session_id,
                    after_msg_id=session.last_summarized_msg_id,
                    limit=20,
                )
                recent_messages = [{"role": m.role, "content": m.content} for m in raw_msgs]
                core_facts = await self.memory_backend.list_facts(status="active", limit=30)
                categories_map = {
                    c.category_key: c for c in await self.memory_backend.list_categories()
                }
                if self.memory_retriever is not None:
                    selected_l1_ids = {
                        f.fact_id
                        for f in core_facts
                        if (categories_map[f.category].inject_to_l1 if f.category in categories_map else f.category != "chitchat")
                    }
                    recalled_memories, retrieval_ms = await self.memory_retriever.search(
                        user_text,
                        top_k=5,
                        exclude_fact_ids=selected_l1_ids,
                    )
                    for item in recalled_memories:
                        if item.item_type == "fact":
                            await self.memory_backend.touch_fact(item.item_id)
            else:
                categories_map = {}
                recent_messages = self.memory_store.get_messages(conn.client_id, limit=16)

            built = self.context_builder.build(
                user_text=user_text,
                user_content=user_content,
                core_facts=core_facts,
                rolling_summary=rolling_summary,
                recent_messages=recent_messages,
                recalled_memories=recalled_memories,
                context_store=self.context_store,
                categories=categories_map if self.memory_backend is not None else None,
                retrieval_ms=retrieval_ms,
            )
            self.last_build_stats = built.stats
            messages = built.messages

            async for chunk in self.llm_router.stream_chat(messages, profile_name=profile.name):
                events = parser.feed(chunk)
                seq = await self._emit_parser_events(conn, turn_id, events, seq, collected_spoken)

            tail_events = parser.finish()
            seq = await self._emit_parser_events(conn, turn_id, tail_events, seq, collected_spoken)

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            assistant_reply = "".join(collected_spoken).strip()
            scene_summary = self.context_store.get_window_summary()

            if self.memory_backend is not None and active_session_id:
                user_rec = await self.memory_backend.append_message(
                    session_id=active_session_id,
                    turn_id=turn_id,
                    role="user",
                    content=user_text,
                    desktop_context_summary=scene_summary,
                )
                assistant_rec = None
                if assistant_reply:
                    assistant_rec = await self.memory_backend.append_message(
                        session_id=active_session_id,
                        turn_id=turn_id,
                        role="assistant",
                        content=assistant_reply,
                        desktop_context_summary=scene_summary,
                    )
                if self.memory_extractor is not None:
                    self.memory_extractor.schedule_post_turn(
                        session_id=active_session_id,
                        user_msg=user_rec,
                        assistant_msg=assistant_rec,
                        context_store=self.context_store,
                    )
            else:
                self.memory_store.append_turn(
                    conn.client_id, "user", user_text, scene_summary=scene_summary
                )
                if assistant_reply:
                    self.memory_store.append_turn(
                        conn.client_id, "assistant", assistant_reply, scene_summary=scene_summary
                    )

            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "output.text_delta",
                    OutputTextDeltaPayload(seq=seq + 1, delta="", is_final=True),
                    turn_id=turn_id,
                )
            )
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "output.turn_end",
                    OutputTurnEndPayload(finish_reason="completed", elapsed_ms=elapsed_ms),
                    turn_id=turn_id,
                )
            )
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "avatar.state",
                    AvatarStatePayload(emotion="neutral", intensity=0.5, activity="idle"),
                    turn_id=turn_id,
                )
            )
        except asyncio.CancelledError:
            logger.info("Turn task cancelled cleanly turn_id=%s", turn_id)
            raise
        except LLMError as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error("Turn failed with LLMError turn_id=%s code=%s", turn_id, exc.code)
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "system.error",
                    SystemErrorPayload(
                        code=exc.code,
                        message=exc.message,
                        recoverable=exc.recoverable,
                    ),
                    turn_id=turn_id,
                )
            )
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "output.turn_end",
                    OutputTurnEndPayload(finish_reason="error", elapsed_ms=elapsed_ms),
                    turn_id=turn_id,
                )
            )
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "avatar.state",
                    AvatarStatePayload(emotion="warning", intensity=0.8, activity="idle"),
                    turn_id=turn_id,
                )
            )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.exception("Unexpected error in turn_id=%s", turn_id)
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "system.error",
                    SystemErrorPayload(
                        code="INTERNAL_ERROR",
                        message=f"大脑处理异常: {type(exc).__name__}",
                        recoverable=True,
                    ),
                    turn_id=turn_id,
                )
            )
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "output.turn_end",
                    OutputTurnEndPayload(finish_reason="error", elapsed_ms=elapsed_ms),
                    turn_id=turn_id,
                )
            )
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "avatar.state",
                    AvatarStatePayload(emotion="warning", intensity=0.8, activity="idle"),
                    turn_id=turn_id,
                )
            )
        finally:
            if artifact_id_to_burn:
                self.artifact_store.delete(artifact_id_to_burn)
            if conn.active_turn_id == turn_id:
                conn.active_turn_id = None
                conn.active_task = None

    async def _emit_parser_events(
        self,
        conn: ClientConnection,
        turn_id: str,
        events: list,
        seq: int,
        collected_spoken: List[str],
    ) -> int:
        for ev in events:
            if isinstance(ev, EmotionEvent):
                await conn.send_envelope(
                    make_envelope(
                        "brain",
                        "avatar.state",
                        AvatarStatePayload(
                            emotion=ev.emotion,
                            intensity=0.8,
                            activity="speaking",
                        ),
                        turn_id=turn_id,
                    )
                )
            elif isinstance(ev, TextEvent):
                seq += 1
                collected_spoken.append(ev.delta)
                await conn.send_envelope(
                    make_envelope(
                        "brain",
                        "output.text_delta",
                        OutputTextDeltaPayload(seq=seq, delta=ev.delta, is_final=False),
                        turn_id=turn_id,
                    )
                )
            elif isinstance(ev, CardEvent):
                actions = []
                if ev.card_type == "spoiler_steps":
                    actions.append(CardAction(id="more_hint", label="还是没头绪，看第 2 级提示"))
                actions.append(CardAction(id="dismiss", label="知道了，收起"))
                await conn.send_envelope(
                    make_envelope(
                        "brain",
                        "output.card",
                        OutputCardPayload(
                            card_id=ev.card_id,
                            card_type=ev.card_type,
                            title=ev.title,
                            content_markdown=ev.content_markdown,
                            actions=actions,
                        ),
                        turn_id=turn_id,
                    )
                )
        return seq
