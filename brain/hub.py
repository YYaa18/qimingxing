from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import WebSocket
from pydantic import ValidationError

from brain.artifact_store import ArtifactStore
from brain.audio import (
    ASRProvider,
    DeepSeekASRProvider,
    DeepSeekTTSProvider,
    SentenceChunker,
    TTSProvider,
    estimate_tts_cost_cny,
)
from brain.config import AppConfig
from brain.context_builder import BuildStats, ContextBuilder
from brain.context_store import ContextStore
from brain.history_store import InMemorySlidingWindowStore, MemoryStore
from brain.intent_detector import ScreenIntentDetector
from brain.llm_router import LLMError, LLMRouter, LLMUsage
from brain.memory.backend import MemoryBackend
from brain.memory.embeddings import NullEmbeddingProvider
from brain.memory.extractor import MemoryExtractor
from brain.memory.models import (
    MemoryFact,
    MemorySkill,
    UsageLedgerRecord,
    WorkHistoryRecord,
    new_work_id,
)
from brain.memory.retrieval import MemoryRetriever
from brain.memory.skills import SkillManager
from brain.screen_router import DeepSeekOCRProvider, ScreenUnderstandingRouter
from brain.stream_parser import CardEvent, EmotionEvent, StreamParser, TextEvent
from brain.tools import (
    BatchPathContextStore,
    ToolExecutionContext,
    ToolExecutor,
    ToolRegistry,
    actions_for_media_type,
    build_default_tool_registry,
    infer_media_type,
)
from brain.tools.base import ToolTraceStep
from brain.tools.desktop_io import AttachPathsResult
from protocol.models import (
    AudioInputChunkPayload,
    AudioInputEndPayload,
    AudioInputStartPayload,
    AudioOutputChunkPayload,
    AudioOutputEndPayload,
    AudioOutputStartPayload,
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
    OutputArtifactCapsulePayload,
    OutputCardPayload,
    OutputProactiveNoticePayload,
    OutputTextDeltaPayload,
    OutputToolStatusPayload,
    OutputTurnEndPayload,
    OutputTurnStartPayload,
    PermissionStatus,
    PrivacyMode,
    RiskLevel,
    SensesCaptureRequestPayload,
    SourceRole,
    SystemCaptureDonePayload,
    SystemCapturePreparePayload,
    SystemConfigSetPayload,
    SystemErrorPayload,
    SystemHelloPayload,
    SystemHotkeyTriggeredPayload,
    SystemPingPayload,
    SystemPongPayload,
    SystemPrivacySetPayload,
    SystemPrivacyStatePayload,
    SystemShortcutsSyncPayload,
    SystemWelcomePayload,
    ToolApprovalRequestPayload,
    ToolApprovalResponsePayload,
    make_envelope,
    new_turn_id,
    parse_envelope,
    parse_payload,
)

logger = logging.getLogger("qimingxing.hub")

SERVER_VERSION = "0.6.0-mvp6"
CAPTURE_TIMEOUT_SECONDS = 3.5
_MAX_TTS_CHARS_PER_TURN = 600


@dataclass
class _AudioInputBuffer:
    turn_id: str
    source_client_id: str
    sample_rate: int = 16000
    channels: int = 1
    pcm_chunks: List[bytes] = field(default_factory=list)
    total_bytes: int = 0
    max_bytes: int = 16000 * 2 * 60


@dataclass
class ClientConnection:
    client_id: str
    websocket: WebSocket
    role: Optional[SourceRole] = None
    client_name: str = "unknown"
    client_type: Optional[str] = None
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
        tool_registry: Optional[ToolRegistry] = None,
        tool_executor: Optional[ToolExecutor] = None,
        skill_manager: Optional[SkillManager] = None,
        asr_provider: Optional[ASRProvider] = None,
        tts_provider: Optional[TTSProvider] = None,
        batch_path_store: Optional[BatchPathContextStore] = None,
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
        self.tool_registry = (
            tool_registry
            if tool_registry is not None
            else build_default_tool_registry(self.config)
        )
        self.tool_executor = tool_executor or ToolExecutor(llm_router)
        if skill_manager is not None:
            self.skill_manager: Optional[SkillManager] = skill_manager
        elif memory_backend is not None:
            self.skill_manager = SkillManager(
                memory_backend, llm_router, self.config.tools.skills
            )
        else:
            self.skill_manager = None
        self.asr_provider: ASRProvider = asr_provider or DeepSeekASRProvider(
            self.config.audio.asr
        )
        self.tts_provider: TTSProvider = tts_provider or DeepSeekTTSProvider(
            self.config.audio.tts
        )
        self.batch_path_store: BatchPathContextStore = (
            batch_path_store or BatchPathContextStore()
        )
        self.capture_timeout_seconds = capture_timeout_seconds

        self.clients: Dict[str, ClientConnection] = {}
        self._conn_seq = 0
        self.privacy_mode: PrivacyMode = self.config.privacy.default_mode
        self.senses_platform: str = "unknown"
        self.senses_permissions = PermissionStatus()
        self.senses_permission_hint: str = ""
        self._pending_captures: Dict[str, asyncio.Future[ContextScreenRefPayload]] = {}
        self._pending_approvals: Dict[str, tuple[str, asyncio.Future[ToolApprovalResponsePayload]]] = {}
        self._turn_cancel_events: Dict[str, asyncio.Event] = {}
        self._active_audio_inputs: Dict[str, _AudioInputBuffer] = {}
        self._latest_audio_turn_by_client: Dict[str, str] = {}
        self._tts_tasks: Dict[str, asyncio.Task[None]] = {}
        self._session_approved_tools: set[str] = set()
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
            conn.client_type = payload.client_type or (
                "body_web" if payload.role == "body" else "senses"
            )
            conn.capabilities = payload.capabilities
            if payload.role == "senses":
                raw_payload = env.payload or {}
                self.senses_platform = str(raw_payload.get("platform", self.senses_platform))
                perms = raw_payload.get("permissions")
                if isinstance(perms, dict):
                    self.senses_permissions = PermissionStatus.model_validate(perms)
                self.senses_permission_hint = str(raw_payload.get("permission_hint", ""))
            logger.info(
                "Handshake hello: client=%s role=%s type=%s name=%s audio_out=%s auto_hide=%s",
                conn.client_id,
                conn.role,
                conn.client_type,
                conn.client_name,
                conn.capabilities.audio_output,
                conn.capabilities.auto_hide,
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

        if isinstance(payload, SystemHotkeyTriggeredPayload):
            logger.info(
                "Received system.hotkey_triggered from %s action=%s hotkey=%s",
                conn.client_id,
                payload.action,
                payload.hotkey,
            )
            if payload.action == "cancel":
                await self._cancel_active_turn_or_bodies(conn, notify=True, target_turn_id=env.turn_id)
            out_env = make_envelope("brain", "system.hotkey_triggered", payload)
            for client in list(self.clients.values()):
                if client.role in ("body", None):
                    try:
                        await client.send_envelope(out_env)
                    except Exception:
                        logger.debug("Failed broadcasting hotkey action to %s", client.client_id)
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

        if isinstance(payload, ToolApprovalResponsePayload):
            entry = self._pending_approvals.get(payload.approval_id)
            if entry is not None:
                _, fut = entry
                if not fut.done():
                    fut.set_result(payload)
            return

        if isinstance(payload, InputCancelPayload):
            target_turn = env.turn_id or conn.active_turn_id
            logger.info(
                "Received input.cancel from %s target_turn=%s reason=%s",
                conn.client_id,
                target_turn,
                payload.reason,
            )
            await self._cancel_active_turn_or_bodies(
                conn, notify=True, target_turn_id=target_turn
            )
            return

        if isinstance(payload, AudioInputStartPayload):
            await self._handle_audio_input_start(conn, env, payload)
            return

        if isinstance(payload, AudioInputChunkPayload):
            await self._handle_audio_input_chunk(conn, env, payload)
            return

        if isinstance(payload, AudioInputEndPayload):
            await self._handle_audio_input_end(conn, env, payload)
            return

        if isinstance(payload, InputTextPayload):
            turn_id = env.turn_id or new_turn_id()
            await self._start_turn(
                conn,
                turn_id,
                payload.text,
                force_screen=False,
                attachments=list(payload.attachments or []),
            )
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
            self._session_approved_tools.clear()
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
            shortcuts=self._build_shortcuts_sync_payload(),
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

    def _build_shortcuts_sync_payload(self) -> SystemShortcutsSyncPayload:
        sc = self.config.shortcuts
        return SystemShortcutsSyncPayload(
            ptt_hotkey=sc.ptt_hotkey or self.config.audio.ptt.hotkey,
            chat_input_hotkey=sc.chat_input_hotkey,
            cancel_hotkey=sc.cancel_hotkey,
            toggle_dock_hotkey=sc.toggle_dock_hotkey,
            settings_hotkey=sc.settings_hotkey,
        )

    async def _send_shortcuts_sync(self, conn: ClientConnection) -> None:
        await conn.send_envelope(
            make_envelope("brain", "system.shortcuts_sync", self._build_shortcuts_sync_payload())
        )

    async def _broadcast_shortcuts_sync(self) -> None:
        payload = self._build_shortcuts_sync_payload()
        for client in list(self.clients.values()):
            if client.role == "senses":
                try:
                    await client.send_envelope(
                        make_envelope("brain", "system.shortcuts_sync", payload)
                    )
                except Exception:
                    logger.debug("Failed sending shortcuts_sync to %s", client.client_id)

    async def _cancel_active_turn_or_bodies(
        self,
        sender_conn: ClientConnection,
        *,
        notify: bool,
        target_turn_id: Optional[str] = None,
    ) -> None:
        if sender_conn.role == "senses":
            for client in list(self.clients.values()):
                if client.role == "body":
                    await self._cancel_active_turn(
                        client, notify=notify, target_turn_id=target_turn_id
                    )
            return
        await self._cancel_active_turn(
            sender_conn, notify=notify, target_turn_id=target_turn_id
        )

    async def _cancel_tts_for_turn(self, turn_id: Optional[str]) -> None:
        if not turn_id:
            return
        tts_task = self._tts_tasks.pop(turn_id, None)
        if tts_task is not None and not tts_task.done():
            tts_task.cancel()
            try:
                await tts_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

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
            if target_turn_id or old_turn_id:
                await self._cancel_tts_for_turn(target_turn_id or old_turn_id)
            return

        if target_turn_id and old_turn_id and target_turn_id != old_turn_id:
            return

        logger.info("Cancelling active turn task turn_id=%s", old_turn_id)
        if old_turn_id and old_turn_id in self._turn_cancel_events:
            self._turn_cancel_events[old_turn_id].set()
        for aid, (pending_tid, fut) in list(self._pending_approvals.items()):
            if pending_tid == old_turn_id and not fut.done():
                fut.cancel()
        await self._cancel_tts_for_turn(old_turn_id)
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

    def _get_primary_body_conn(
        self, fallback_conn: Optional[ClientConnection] = None
    ) -> Optional[ClientConnection]:
        bodies = [c for c in self.clients.values() if c.role == "body"]
        for c in bodies:
            if c.client_type == "body_godot" or c.capabilities.audio_output:
                return c
        if bodies:
            return bodies[0]
        return fallback_conn

    def _get_tts_target_conns(self, primary_conn: ClientConnection) -> List[ClientConnection]:
        targets: List[ClientConnection] = []
        seen: set[str] = set()
        for c in (primary_conn, *self.clients.values()):
            if c.client_id in seen:
                continue
            seen.add(c.client_id)
            if c.role == "body" and c.capabilities.audio_output:
                targets.append(c)
        return targets

    def attach_context_paths(
        self,
        raw_paths: List[str],
        *,
        source: str = "manual",
        append: bool = True,
    ) -> AttachPathsResult:
        return self.batch_path_store.attach(
            raw_paths,
            source=source,
            append=append,
            max_paths=self.config.batch_context.max_paths,
            max_tree_depth=self.config.batch_context.max_tree_depth,
        )

    async def apply_config_update(self, fresh_cfg: AppConfig) -> None:
        """Apply hot-reloaded settings (API keys, profiles, audio, privacy, godot) across Hub."""
        self.config = fresh_cfg
        self.llm_router.reload_config(fresh_cfg)
        self.privacy_mode = fresh_cfg.privacy.default_mode
        if hasattr(self.asr_provider, "config"):
            self.asr_provider.config = fresh_cfg.audio.asr  # type: ignore[attr-defined]
        if hasattr(self.tts_provider, "config"):
            self.tts_provider.config = fresh_cfg.audio.tts  # type: ignore[attr-defined]
        if hasattr(self.screen_router, "ocr_provider") and hasattr(
            self.screen_router.ocr_provider, "config"
        ):
            self.screen_router.ocr_provider.config = fresh_cfg.ocr  # type: ignore[attr-defined]
        inbox_tool = self.tool_registry.get("inbox_scan")
        if inbox_tool is not None and hasattr(inbox_tool, "config"):
            inbox_tool.config = fresh_cfg  # type: ignore[attr-defined]
        await self._broadcast_welcome_to_bodies()
        await self._broadcast_privacy_state()
        await self._broadcast_shortcuts_sync()

    async def broadcast_tool_notice(self, tool: str, label: str) -> None:
        env = make_envelope(
            "brain",
            "output.tool_status",
            OutputToolStatusPayload(status="completed", tool=tool, label=label),
        )
        for client in list(self.clients.values()):
            if client.role in ("body", None):
                try:
                    await client.send_envelope(env)
                except Exception:
                    logger.debug("Failed broadcasting tool notice to %s", client.client_id)

    async def emit_artifact_capsule(
        self,
        conn: ClientConnection,
        *,
        turn_id: Optional[str],
        path: str,
        title: Optional[str] = None,
    ) -> None:
        p = Path(os.path.abspath(os.path.expanduser(path)))
        mtype = infer_media_type(p)
        actions = actions_for_media_type(mtype)
        payload = OutputArtifactCapsulePayload(
            turn_id=turn_id,
            title=title or p.name or "产物文件",
            path=str(p),
            media_type=mtype,
            actions=actions,
        )
        await conn.send_envelope(
            make_envelope("brain", "output.artifact_capsule", payload, turn_id=turn_id)
        )

    async def handle_raw_bytes(self, conn: ClientConnection, raw_bytes: bytes) -> None:
        """Accept raw binary WS PCM chunk routed to client's active audio input session."""
        tid = self._latest_audio_turn_by_client.get(conn.client_id)
        if not tid:
            return
        buf = self._active_audio_inputs.get(tid)
        if buf is None:
            return
        remaining = max(0, buf.max_bytes - buf.total_bytes)
        if remaining > 0 and raw_bytes:
            sliced = raw_bytes[:remaining]
            buf.pcm_chunks.append(sliced)
            buf.total_bytes += len(sliced)

    async def _handle_audio_input_start(
        self,
        conn: ClientConnection,
        env: Envelope,
        payload: AudioInputStartPayload,
    ) -> None:
        # Barge-in immediately on any active speech / turn
        await self._cancel_active_turn_or_bodies(conn, notify=True, target_turn_id=None)
        tid = payload.turn_id or env.turn_id or new_turn_id()
        sr = max(8000, int(payload.sample_rate or self.config.audio.ptt.sample_rate))
        ch = max(1, int(payload.channels or 1))
        max_bytes = sr * ch * 2 * max(5, int(self.config.audio.ptt.max_record_sec))
        self._active_audio_inputs[tid] = _AudioInputBuffer(
            turn_id=tid,
            source_client_id=conn.client_id,
            sample_rate=sr,
            channels=ch,
            max_bytes=max_bytes,
        )
        self._latest_audio_turn_by_client[conn.client_id] = tid
        target = self._get_primary_body_conn(fallback_conn=conn)
        if target is not None:
            try:
                await target.send_envelope(
                    make_envelope(
                        "brain",
                        "avatar.state",
                        AvatarStatePayload(emotion="neutral", intensity=0.8, activity="thinking"),
                        turn_id=tid,
                    )
                )
            except Exception:
                pass

    async def _handle_audio_input_chunk(
        self,
        conn: ClientConnection,
        env: Envelope,
        payload: AudioInputChunkPayload,
    ) -> None:
        tid = payload.turn_id or env.turn_id or self._latest_audio_turn_by_client.get(conn.client_id)
        if not tid:
            return
        buf = self._active_audio_inputs.get(tid)
        if buf is None or not payload.data_b64:
            return
        try:
            chunk = base64.b64decode(payload.data_b64)
        except Exception:
            return
        remaining = max(0, buf.max_bytes - buf.total_bytes)
        if remaining > 0 and chunk:
            sliced = chunk[:remaining]
            buf.pcm_chunks.append(sliced)
            buf.total_bytes += len(sliced)

    async def _handle_audio_input_end(
        self,
        conn: ClientConnection,
        env: Envelope,
        payload: AudioInputEndPayload,
    ) -> None:
        tid = payload.turn_id or env.turn_id or self._latest_audio_turn_by_client.get(conn.client_id)
        if not tid:
            return
        buf = self._active_audio_inputs.pop(tid, None)
        if self._latest_audio_turn_by_client.get(conn.client_id) == tid:
            self._latest_audio_turn_by_client.pop(conn.client_id, None)
        if buf is None:
            return

        target = self._get_primary_body_conn(fallback_conn=conn)
        if target is None:
            return

        if not self.config.audio.enabled:
            await self._emit_direct_guidance(
                target,
                tid,
                "语音功能当前已在配置中关闭。",
                emotion="neutral",
            )
            return

        pcm_bytes = b"".join(buf.pcm_chunks)
        if not pcm_bytes:
            await target.send_envelope(
                make_envelope(
                    "brain",
                    "avatar.state",
                    AvatarStatePayload(emotion="neutral", intensity=0.5, activity="idle"),
                    turn_id=tid,
                )
            )
            return

        await target.send_envelope(
            make_envelope(
                "brain",
                "output.tool_status",
                OutputToolStatusPayload(status="running", tool="asr", label="正在识别语音…"),
                turn_id=tid,
            )
        )
        try:
            asr_res = await self.asr_provider.transcribe_pcm(
                pcm_bytes, sample_rate=buf.sample_rate, channels=buf.channels
            )
        except Exception as exc:
            safe_err = self.config.redact_secrets(f"{type(exc).__name__}: {exc}")
            logger.warning("ASR transcription failed (falling back to text bubble): %s", safe_err)
            await target.send_envelope(
                make_envelope(
                    "brain",
                    "output.tool_status",
                    OutputToolStatusPayload(status="error", tool="asr", label="语音识别失败，请改用文字或重试"),
                    turn_id=tid,
                )
            )
            await target.send_envelope(
                make_envelope(
                    "brain",
                    "avatar.state",
                    AvatarStatePayload(emotion="confused", intensity=0.7, activity="idle"),
                    turn_id=tid,
                )
            )
            return

        if self.memory_backend is not None and asr_res.estimated_cost_cny > 0:
            active_session_id = ""
            try:
                sess = await self.memory_backend.get_or_create_active_session()
                active_session_id = sess.session_id
            except Exception:
                pass
            await self._record_usage_and_alert(
                conn=target,
                turn_id=tid,
                session_id=active_session_id,
                turn_usages=[
                    LLMUsage(
                        profile="audio_asr",
                        model=self.config.audio.asr.model,
                        estimated_cost_cny=asr_res.estimated_cost_cny,
                    )
                ],
            )

        transcript = (asr_res.text or "").strip()
        if not transcript:
            await target.send_envelope(
                make_envelope(
                    "brain",
                    "output.tool_status",
                    OutputToolStatusPayload(status="completed", tool="asr", label="未检测到清晰语音"),
                    turn_id=tid,
                )
            )
            await target.send_envelope(
                make_envelope(
                    "brain",
                    "avatar.state",
                    AvatarStatePayload(emotion="neutral", intensity=0.5, activity="idle"),
                    turn_id=tid,
                )
            )
            return

        await target.send_envelope(
            make_envelope(
                "brain",
                "output.tool_status",
                OutputToolStatusPayload(status="completed", tool="asr", label=f"我听到：「{transcript[:60]}」"),
                turn_id=tid,
            )
        )
        await self._start_turn(target, tid, transcript, force_screen=False)

    async def _run_tts_worker(
        self,
        *,
        turn_id: str,
        targets: List[ClientConnection],
        queue: asyncio.Queue[Optional[str]],
        cancel_event: asyncio.Event,
        session_id: str,
        usage_conn: ClientConnection,
    ) -> None:
        started = False
        seq = 0
        sentence_index = 0
        finish_reason: str = "completed"
        sample_rate = max(8000, int(self.config.audio.tts.sample_rate or 24000))

        try:
            while True:
                sentence = await queue.get()
                if sentence is None:
                    break
                if cancel_event.is_set():
                    finish_reason = "cancelled"
                    break

                if not started:
                    start_env = make_envelope(
                        "brain",
                        "audio.output_start",
                        AudioOutputStartPayload(
                            turn_id=turn_id,
                            sample_rate=sample_rate,
                            channels=1,
                            format="pcm_s16le",
                        ),
                        turn_id=turn_id,
                    )
                    for t in targets:
                        await t.send_envelope(start_env)
                    started = True

                async for pcm_chunk in self.tts_provider.synthesize_pcm_stream(
                    sentence, voice=self.config.audio.tts.voice
                ):
                    if cancel_event.is_set():
                        finish_reason = "cancelled"
                        raise asyncio.CancelledError
                    if not pcm_chunk:
                        continue
                    seq += 1
                    chunk_env = make_envelope(
                        "brain",
                        "audio.output_chunk",
                        AudioOutputChunkPayload(
                            turn_id=turn_id,
                            seq=seq,
                            sentence_index=sentence_index,
                            data_b64=base64.b64encode(pcm_chunk).decode("ascii"),
                            sample_rate=sample_rate,
                            format="pcm_s16le",
                        ),
                        turn_id=turn_id,
                    )
                    for t in targets:
                        await t.send_envelope(chunk_env)

                if self.memory_backend is not None:
                    tts_cost = estimate_tts_cost_cny(sentence)
                    if tts_cost > 0:
                        await self._record_usage_and_alert(
                            conn=usage_conn,
                            turn_id=turn_id,
                            session_id=session_id,
                            turn_usages=[
                                LLMUsage(
                                    profile="audio_tts",
                                    model=self.config.audio.tts.model,
                                    estimated_cost_cny=tts_cost,
                                )
                            ],
                        )
                sentence_index += 1
        except asyncio.CancelledError:
            finish_reason = "cancelled"
            raise
        except Exception as exc:
            finish_reason = "error"
            safe_err = self.config.redact_secrets(f"{type(exc).__name__}: {exc}")
            logger.warning("TTS synthesis degraded gracefully (%s): %s", turn_id, safe_err)
        finally:
            self._tts_tasks.pop(turn_id, None)
            if started:
                end_env = make_envelope(
                    "brain",
                    "audio.output_end",
                    AudioOutputEndPayload(
                        turn_id=turn_id,
                        finish_reason=finish_reason,  # type: ignore[arg-type]
                    ),
                    turn_id=turn_id,
                )
                for t in targets:
                    try:
                        await t.send_envelope(end_env)
                    except Exception:
                        pass

    async def broadcast_proactive_notice(
        self, notice: OutputProactiveNoticePayload
    ) -> None:
        env = make_envelope("brain", "output.proactive_notice", notice)
        for client in list(self.clients.values()):
            if client.role in ("body", None):
                try:
                    await client.send_envelope(env)
                except Exception:
                    logger.debug("Failed broadcasting proactive_notice to %s", client.client_id)

    async def broadcast_soft_alert(self, message: str) -> None:
        env = make_envelope(
            "brain",
            "system.error",
            SystemErrorPayload(code="USAGE_SOFT_ALERT", message=message, recoverable=True),
        )
        for client in list(self.clients.values()):
            if client.role in ("body", None):
                try:
                    await client.send_envelope(env)
                except Exception:
                    logger.debug("Failed broadcasting soft_alert to %s", client.client_id)

    async def _start_turn(
        self,
        conn: ClientConnection,
        turn_id: str,
        user_text: str,
        *,
        force_screen: bool,
        attachments: Optional[List[str]] = None,
    ) -> None:
        await self._cancel_active_turn(conn, notify=True)
        conn.active_turn_id = turn_id
        task = asyncio.create_task(
            self._run_turn(
                conn,
                turn_id,
                user_text,
                force_screen=force_screen,
                attachments=attachments,
            )
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
            auto_hide_bodies = [
                c
                for c in self.clients.values()
                if c.role == "body" and c.capabilities.auto_hide
            ]
            hid_bodies = False
            if self.config.godot.auto_hide_on_capture and auto_hide_bodies:
                prep_env = make_envelope(
                    "brain",
                    "system.capture_prepare",
                    SystemCapturePreparePayload(
                        request_id=req_id, turn_id=turn_id, hide_ms=150
                    ),
                    turn_id=turn_id,
                )
                for body_conn in auto_hide_bodies:
                    try:
                        await body_conn.send_envelope(prep_env)
                        hid_bodies = True
                    except Exception:
                        pass
                if hid_bodies:
                    await asyncio.sleep(0.15)

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
            if self.config.godot.auto_hide_on_capture:
                done_env = make_envelope(
                    "brain",
                    "system.capture_done",
                    SystemCaptureDonePayload(request_id=req_id, turn_id=turn_id),
                    turn_id=turn_id,
                )
                for body_conn in list(self.clients.values()):
                    if body_conn.role == "body" and body_conn.capabilities.auto_hide:
                        try:
                            await body_conn.send_envelope(done_env)
                        except Exception:
                            pass

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

    async def _request_tool_approval(
        self,
        *,
        conn: ClientConnection,
        turn_id: str,
        tool_name: str,
        risk_level: RiskLevel,
        summary: str,
        details: Dict[str, Any],
        timeout_sec: int,
    ) -> tuple[bool, bool]:
        approval_id = f"apr_{uuid.uuid4().hex[:10]}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ToolApprovalResponsePayload] = loop.create_future()
        self._pending_approvals[approval_id] = (turn_id, fut)
        try:
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "tool.approval_request",
                    ToolApprovalRequestPayload(
                        approval_id=approval_id,
                        turn_id=turn_id,
                        tool_name=tool_name,
                        risk_level=risk_level,
                        summary=summary,
                        details=details,
                        timeout_sec=timeout_sec,
                    ),
                    turn_id=turn_id,
                )
            )
            resp = await asyncio.wait_for(fut, timeout=max(1, timeout_sec))
            return bool(resp.approved), bool(resp.remember_for_session)
        except asyncio.TimeoutError:
            logger.info("Tool approval timed out approval_id=%s tool=%s", approval_id, tool_name)
            return False, False
        finally:
            self._pending_approvals.pop(approval_id, None)

    async def _record_usage_and_alert(
        self,
        *,
        conn: ClientConnection,
        turn_id: str,
        session_id: str,
        turn_usages: List[LLMUsage],
    ) -> None:
        if self.memory_backend is None or not turn_usages:
            return
        turn_tokens = 0
        for u in turn_usages:
            if (
                u.total_tokens <= 0
                and u.prompt_tokens <= 0
                and u.completion_tokens <= 0
                and u.estimated_cost_cny <= 0
            ):
                continue
            turn_tokens += u.total_tokens or (u.prompt_tokens + u.completion_tokens)
            await self.memory_backend.add_usage_record(
                UsageLedgerRecord(
                    session_id=session_id,
                    turn_id=turn_id,
                    profile=u.profile,
                    model=u.model,
                    prompt_tokens=u.prompt_tokens,
                    prompt_cache_hit_tokens=u.prompt_cache_hit_tokens,
                    completion_tokens=u.completion_tokens,
                    estimated_cost_cny=u.estimated_cost_cny,
                )
            )
        alert_cfg = self.config.tools.usage_alert
        if not alert_cfg.enabled:
            return
        lt = time.localtime()
        day_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        summary = await self.memory_backend.get_usage_summary(since_ts=day_start)
        reasons: List[str] = []
        if alert_cfg.turn_warn_tokens > 0 and turn_tokens >= alert_cfg.turn_warn_tokens:
            reasons.append(f"单轮消耗 {turn_tokens} tokens（提醒阈值 {alert_cfg.turn_warn_tokens}）")
        if alert_cfg.daily_warn_cny > 0 and summary.total_cost_cny >= alert_cfg.daily_warn_cny:
            reasons.append(
                f"今日累计估算 ¥{summary.total_cost_cny:.4f}（提醒阈值 ¥{alert_cfg.daily_warn_cny:.2f}）"
            )
        if reasons:
            msg = (
                f"用量软提醒：{'；'.join(reasons)}。"
                f"当前缓存命中率 {summary.cache_hit_rate * 100:.1f}%，已按策略继续放行后续对话。"
            )
            logger.warning("Usage soft alert (%s): %s", turn_id, msg)
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "system.error",
                    SystemErrorPayload(
                        code="USAGE_SOFT_ALERT",
                        message=msg,
                        recoverable=True,
                    ),
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
        attachments: Optional[List[str]] = None,
    ) -> None:
        started = time.perf_counter()
        profile = self.llm_router.get_profile()
        parser = StreamParser()
        seq = 0
        collected_spoken: List[str] = []
        artifact_id_to_burn = ""
        artifacts_to_burn: List[str] = list(attachments or [])
        extra_l3_sections: List[str] = []
        cancel_event = asyncio.Event()
        self._turn_cancel_events[turn_id] = cancel_event

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

            wants_screen = force_screen or (
                not attachments and self.intent_detector.detect(user_text)
            )
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

            if attachments:
                extra_image_parts: List[Dict[str, Any]] = []
                for aid in attachments:
                    rec = self.artifact_store.get_record(aid)
                    if rec is None:
                        extra_l3_sections.append(f"【附件提醒】临时附件 {aid} 不存在或已过期清理。")
                        continue
                    if rec.kind == "image":
                        loaded_att = self.artifact_store.read_bytes(aid)
                        if loaded_att is None:
                            continue
                        att_bytes, att_ctype = loaded_att
                        if not profile.supports_vision:
                            await conn.send_envelope(
                                make_envelope(
                                    "brain",
                                    "output.tool_status",
                                    OutputToolStatusPayload(
                                        status="running",
                                        tool="deepseek_ocr",
                                        label=f"正在用云端 OCR 解析图片附件 {rec.filename}…",
                                    ),
                                    turn_id=turn_id,
                                )
                            )
                        prepared_att, _ = await self.screen_router.prepare_user_message(
                            user_text="",
                            image_bytes=att_bytes,
                            content_type=att_ctype,
                            profile=profile,
                            screen_ref=ContextScreenRefPayload(
                                request_id=aid,
                                turn_id=turn_id,
                                status="ok",
                                artifact_id=aid,
                                target_app="用户直投图片",
                                target_title=rec.filename,
                            ),
                        )
                        if isinstance(prepared_att, list):
                            for part in prepared_att:
                                if part.get("type") == "image_url":
                                    extra_image_parts.append(part)
                            extra_l3_sections.append(f"【用户直投图片附件】{rec.filename}（已附带在多模态输入中）")
                        else:
                            extra_l3_sections.append(
                                f"【用户直投图片 OCR 识别 ({rec.filename})】\n{prepared_att}"
                            )
                    else:
                        extracted, err_msg = self.artifact_store.extract_text(
                            aid, max_chars=8000, redact_fn=self.config.redact_secrets
                        )
                        if err_msg:
                            extra_l3_sections.append(
                                f"【附件解析失败 ({rec.filename})】\n{err_msg}"
                            )
                        else:
                            extra_l3_sections.append(
                                f"【用户直投附件 ({rec.kind}: {rec.filename})】\n{extracted}"
                            )

                if extra_image_parts:
                    if isinstance(user_content, list):
                        user_content = list(user_content) + extra_image_parts
                    else:
                        user_content = [{"type": "text", "text": str(user_content)}] + extra_image_parts

            attached_manifest = self.batch_path_store.consume_for_l3()
            if attached_manifest:
                extra_l3_sections.append(attached_manifest)

            active_session_id = ""
            rolling_summary = ""
            core_facts: List[MemoryFact] = []
            recalled_memories = []
            recalled_skills: List[MemorySkill] = []
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
                if self.skill_manager is not None:
                    recalled_skills = await self.skill_manager.recall_skills(user_text)
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
                recalled_skills=recalled_skills,
                extra_l3_sections=extra_l3_sections,
                retrieval_ms=retrieval_ms,
            )
            self.last_build_stats = built.stats
            messages = built.messages

            turn_usages: List[LLMUsage] = []
            trace_steps: List[ToolTraceStep] = []

            tts_targets = (
                self._get_tts_target_conns(conn) if self.config.audio.enabled else []
            )
            chunker: Optional[SentenceChunker] = (
                SentenceChunker() if tts_targets else None
            )
            tts_queue: Optional[asyncio.Queue[Optional[str]]] = (
                asyncio.Queue() if tts_targets else None
            )
            tts_chars_sent = 0
            tts_task: Optional[asyncio.Task[None]] = None
            if tts_targets and tts_queue is not None:
                tts_task = asyncio.create_task(
                    self._run_tts_worker(
                        turn_id=turn_id,
                        targets=tts_targets,
                        queue=tts_queue,
                        cancel_event=cancel_event,
                        session_id=active_session_id,
                        usage_conn=conn,
                    )
                )
                self._tts_tasks[turn_id] = tts_task

            def _enqueue_tts_delta(delta_text: str) -> None:
                nonlocal tts_chars_sent
                if chunker is None or tts_queue is None:
                    return
                for sent in chunker.feed(delta_text):
                    if tts_chars_sent >= _MAX_TTS_CHARS_PER_TURN:
                        break
                    tts_queue.put_nowait(sent)
                    tts_chars_sent += len(sent)

            use_tools = bool(
                self.config.tools.enabled
                and len(self.tool_registry) > 0
                and (
                    profile.provider != "mock"
                    or type(self.llm_router).complete_with_tools is not LLMRouter.complete_with_tools
                )
            )

            if use_tools:
                async def _on_text_delta(chunk: str) -> None:
                    nonlocal seq
                    events = parser.feed(chunk)
                    seq = await self._emit_parser_events(
                        conn, turn_id, events, seq, collected_spoken, on_spoken_delta=_enqueue_tts_delta
                    )

                async def _on_status(
                    status: str,
                    tool_name: str,
                    label: str,
                    step: Optional[int],
                    max_steps: Optional[int],
                ) -> None:
                    await conn.send_envelope(
                        make_envelope(
                            "brain",
                            "output.tool_status",
                            OutputToolStatusPayload(
                                status=status,  # type: ignore[arg-type]
                                tool=tool_name,
                                label=label,
                                step=step,
                                max_steps=max_steps,
                            ),
                            turn_id=turn_id,
                        )
                    )
                    if status == "running":
                        await conn.send_envelope(
                            make_envelope(
                                "brain",
                                "avatar.state",
                                AvatarStatePayload(
                                    emotion="thinking",
                                    intensity=0.75,
                                    activity="tool_running",
                                ),
                                turn_id=turn_id,
                            )
                        )

                async def _on_approval(
                    tool_name: str,
                    risk_level: RiskLevel,
                    summary: str,
                    details: Dict[str, Any],
                    timeout_sec: int,
                ) -> tuple[bool, bool]:
                    return await self._request_tool_approval(
                        conn=conn,
                        turn_id=turn_id,
                        tool_name=tool_name,
                        risk_level=risk_level,
                        summary=summary,
                        details=details,
                        timeout_sec=timeout_sec,
                    )

                tool_ctx = ToolExecutionContext(
                    turn_id=turn_id,
                    session_id=active_session_id,
                    config=self.config,
                    llm_router=self.llm_router,
                    memory_backend=self.memory_backend,
                    context_store=self.context_store,
                    cancel_event=cancel_event,
                    session_approved_tools=self._session_approved_tools,
                    on_status=_on_status,
                    on_approval=_on_approval,
                )
                loop_res = await self.tool_executor.run(
                    messages=messages,
                    registry=self.tool_registry,
                    ctx=tool_ctx,
                    profile_name=profile.name,
                    on_text_delta=_on_text_delta,
                )
                trace_steps = loop_res.trace_steps
                turn_usages = loop_res.usages
            else:
                async for chunk in self.llm_router.stream_chat(messages, profile_name=profile.name):
                    events = parser.feed(chunk)
                    seq = await self._emit_parser_events(
                        conn, turn_id, events, seq, collected_spoken, on_spoken_delta=_enqueue_tts_delta
                    )
                if self.llm_router.last_usage is not None:
                    turn_usages.append(self.llm_router.last_usage)

            tail_events = parser.finish()
            seq = await self._emit_parser_events(
                conn, turn_id, tail_events, seq, collected_spoken, on_spoken_delta=_enqueue_tts_delta
            )

            if chunker is not None and tts_queue is not None:
                for sent in chunker.flush():
                    if tts_chars_sent >= _MAX_TTS_CHARS_PER_TURN:
                        break
                    tts_queue.put_nowait(sent)
                    tts_chars_sent += len(sent)
                tts_queue.put_nowait(None)

            for s in trace_steps:
                if s.status == "ok" and any(
                    k in s.tool_name.lower() for k in ("write", "create", "save", "export", "generate")
                ):
                    raw_out_path = str(s.arguments.get("path") or "").strip()
                    if raw_out_path and os.path.isfile(os.path.expanduser(raw_out_path)):
                        try:
                            await self.emit_artifact_capsule(
                                conn, turn_id=turn_id, path=raw_out_path
                            )
                        except Exception:
                            pass

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            assistant_reply = "".join(collected_spoken).strip()
            scene_summary = self.context_store.get_window_summary()

            if self.memory_backend is not None:
                await self._record_usage_and_alert(
                    conn=conn,
                    turn_id=turn_id,
                    session_id=active_session_id,
                    turn_usages=turn_usages,
                )
                actionable_steps = [
                    s for s in trace_steps if s.tool_name != "work_history_inspect"
                ]
                if actionable_steps:
                    import json as _json

                    trace_payload = [
                        {
                            "step": s.step,
                            "tool": s.tool_name,
                            "args": s.arguments,
                            "risk": s.risk_level,
                            "status": s.status,
                            "summary": s.summary,
                            "elapsed_ms": s.elapsed_ms,
                        }
                        for s in actionable_steps
                    ]
                    work_status = (
                        "error"
                        if any(s.status in ("error", "blocked") for s in actionable_steps)
                        and not any(s.status == "ok" for s in actionable_steps)
                        else "completed"
                    )
                    await self.memory_backend.add_work_history(
                        WorkHistoryRecord(
                            work_id=new_work_id(),
                            session_id=active_session_id,
                            turn_id=turn_id,
                            goal=user_text[:200],
                            result_summary=assistant_reply[:300] or "已完成工具执行",
                            trace_json=_json.dumps(trace_payload, ensure_ascii=False),
                            step_count=len(actionable_steps),
                            status=work_status,
                        )
                    )
                    if self.skill_manager is not None:
                        self.skill_manager.schedule_learn_from_trace(
                            user_text=user_text,
                            trace_steps=actionable_steps,
                            final_summary=assistant_reply[:300],
                        )
                        if recalled_skills:
                            had_tool_error = any(s.status == "error" for s in actionable_steps)
                            if not had_tool_error and any(s.status == "ok" for s in actionable_steps):
                                await self.skill_manager.record_feedback(
                                    recalled_skills[0].skill_id, success=True
                                )
                            elif had_tool_error and not any(
                                s.status == "ok" for s in actionable_steps
                            ):
                                await self.skill_manager.record_feedback(
                                    recalled_skills[0].skill_id, success=False
                                )

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

            if tts_task is not None:
                await tts_task

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
            self._turn_cancel_events.pop(turn_id, None)
            await self._cancel_tts_for_turn(turn_id)
            if artifact_id_to_burn:
                self.artifact_store.delete(artifact_id_to_burn)
            for aid in artifacts_to_burn:
                self.artifact_store.delete(aid)
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
        on_spoken_delta: Optional[Any] = None,
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
                if on_spoken_delta is not None:
                    on_spoken_delta(ev.delta)
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
