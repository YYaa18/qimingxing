from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket
from pydantic import ValidationError

from brain.artifact_store import ArtifactStore
from brain.audio import (
    ASRProvider,
    RoutedASRProvider,
    RoutedTTSProvider,
    SentenceChunker,
    TTSProvider,
    estimate_tts_cost_cny,
)
from brain.config import AppConfig
from brain.context_budget import estimate_tools_tokens, resolve_context_budget
from brain.context_builder import BuildStats, ContextBuilder
from brain.context_store import ContextStore
from brain.history_store import InMemorySlidingWindowStore, MemoryStore
from brain.intent_detector import ScreenIntentDetector
from brain.intent_resolver import build_capability_catalog, build_intent_hint
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
from brain.prompts import BASE_SYSTEM_PROMPT
from brain.rich_content import (
    clean_answer_and_sources,
    collect_media,
    extract_source_snippets,
    extract_table_media,
    label_sources_with_llm,
    needs_source_labeling,
)
from protocol.models import MediaItem, SourceItem
from brain.screen_router import DeepSeekOCRProvider, ScreenUnderstandingRouter
from brain.stream_parser import CardEvent, EmotionEvent, StreamParser, TextEvent
from brain.worker import build_router_registry
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
from brain.utils import get_app_version
from brain.hub.connection import ClientConnection
from brain.hub.approvals import ApprovalMixin
from brain.hub.turns import TurnStreamMixin
from brain.hub.ui_state import UIStateMixin
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
    TaskFollowupAckedPayload,
    TaskStateChangedPayload,
    TaskStatusPayload,
    TaskWatchdogAlertPayload,
    ToolApprovalRequestPayload,
    ToolApprovalResolvedPayload,
    ToolApprovalResponsePayload,
    UICommandPayload,
    UICommandResultPayload,
    UISetStatePayload,
    UIStatePayload,
    make_envelope,
    new_turn_id,
    parse_envelope,
    parse_payload,
)

logger = logging.getLogger("qimingxing.hub")


SERVER_VERSION = get_app_version()
CAPTURE_TIMEOUT_SECONDS = 3.5
RECONNECT_APPROVAL_GRACE_SECONDS = 120.0
_DANGEROUS_OPEN_EXTENSIONS = {
    ".sh",
    ".command",
    ".app",
    ".pkg",
    ".dmg",
    ".scpt",
    ".applescript",
    ".py",
    ".rb",
    ".pl",
    ".exe",
    ".bat",
    ".ps1",
    ".workflow",
}


@dataclass
class _AudioInputBuffer:
    turn_id: str
    source_client_id: str
    sample_rate: int = 16000
    channels: int = 1
    pcm_chunks: List[bytes] = field(default_factory=list)
    total_bytes: int = 0
    max_bytes: int = 16000 * 2 * 60



class Hub(UIStateMixin, ApprovalMixin, TurnStreamMixin):
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
        task_runner: Optional[Any] = None,
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
            DeepSeekOCRProvider(self.config.ocr, self.config),
            config=self.config,
        )
        self.tool_registry = (
            tool_registry
            if tool_registry is not None
            else build_default_tool_registry(self.config)
        )
        self.router_include_mcp = False
        self.router_extra_tool_names: Set[str] = set()
        self.router_tool_registry = build_router_registry(
            self.tool_registry,
            extra_tool_names=self.router_extra_tool_names,
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
        self.asr_provider: ASRProvider = asr_provider or RoutedASRProvider(
            self.config.audio.asr
        )
        self.tts_provider: TTSProvider = tts_provider or RoutedTTSProvider(
            self.config.audio.tts
        )
        self.batch_path_store: BatchPathContextStore = (
            batch_path_store or BatchPathContextStore()
        )
        self.task_runner: Optional[Any] = task_runner
        self.capture_timeout_seconds = capture_timeout_seconds

        self.clients: Dict[str, ClientConnection] = {}
        self._conn_seq = 0
        self.privacy_mode: PrivacyMode = self.config.privacy.default_mode
        self.senses_platform: str = "unknown"
        self.senses_permissions = PermissionStatus()
        self.senses_permission_hint: str = ""
        self._pending_captures: Dict[str, asyncio.Future[ContextScreenRefPayload]] = {}
        self._pending_approvals: Dict[str, tuple[str, asyncio.Future[ToolApprovalResponsePayload]]] = {}
        self._pending_approval_requests: Dict[str, ToolApprovalRequestPayload] = {}
        self._approval_responders: Dict[str, tuple[str, str]] = {}
        self._command_requesters: Dict[str, str] = {}
        self._ui_command_futures: Dict[str, asyncio.Future[UICommandResultPayload]] = {}
        self._orphaned_conns: Dict[str, ClientConnection] = {}
        self._conn_aliases: Dict[str, ClientConnection] = {}
        self._turn_cancel_events: Dict[str, asyncio.Event] = {}
        self._turn_end_sent: Set[str] = set()
        self._active_audio_inputs: Dict[str, _AudioInputBuffer] = {}
        self._latest_audio_turn_by_client: Dict[str, str] = {}
        self._tts_tasks: Dict[str, asyncio.Task[None]] = {}
        # Approval "remember" is scoped per client identity, not process-wide.
        self._approved_tools_by_client: Dict[str, set[str]] = {}
        # card_id -> allowed action ids. Cards stay clickable after the turn
        # ends, so entries are evicted FIFO by capacity, not per turn.
        self._card_actions: Dict[str, set[str]] = {}
        self._card_action_limit = 200
        self.last_build_stats: Optional[BuildStats] = None

    def register(self, websocket: WebSocket) -> ClientConnection:
        self._conn_seq += 1
        client_id = f"client_{self._conn_seq}"
        conn = ClientConnection(client_id=client_id, websocket=websocket)
        self.clients[client_id] = conn
        return conn

    async def unregister(
        self,
        conn: ClientConnection,
        websocket: Optional[WebSocket] = None,
    ) -> None:
        transport_id = conn.client_id
        resolved = self._resolve_conn(conn)
        if resolved is None:
            return
        # A late disconnect from the old transport must not tear down a
        # connection that has already been rebound to a new WebSocket.
        if websocket is not None and resolved.websocket is not websocket:
            return
        self._conn_aliases.pop(transport_id, None)
        conn = resolved
        self._conn_aliases = {
            key: value for key, value in self._conn_aliases.items() if value is not conn
        }

        instance_id = conn.client_instance_id
        if (
            instance_id
            and conn.active_turn_id
            and self._has_pending_approval_for_turn(conn.active_turn_id)
        ):
            # Keep the turn (and its approval future) alive for a short window so
            # the same shell instance can reconnect, receive ui.state, and answer.
            conn.disconnected = True
            self.clients.pop(conn.client_id, None)
            self._orphaned_conns[instance_id] = conn
            if conn.grace_task is not None and not conn.grace_task.done():
                conn.grace_task.cancel()
            conn.grace_task = asyncio.create_task(
                self._expire_orphaned_conn(instance_id, conn, conn.active_turn_id)
            )
            logger.info(
                "Disconnected client=%s while awaiting approval; keeping turn=%s for %.0fs (instance=%s)",
                conn.client_id,
                conn.active_turn_id,
                RECONNECT_APPROVAL_GRACE_SECONDS,
                instance_id,
            )
            return

        await self._cancel_active_turn(conn, notify=False)
        self.clients.pop(conn.client_id, None)
        if instance_id:
            self._orphaned_conns.pop(instance_id, None)
        logger.info("Disconnected client=%s role=%s", conn.client_id, conn.role)
        if conn.role == "senses":
            await self._broadcast_privacy_state()

    def _resolve_conn(self, conn: Optional[ClientConnection]) -> Optional[ClientConnection]:
        if conn is None:
            return None
        return self._conn_aliases.get(conn.client_id, conn)

    async def _expire_orphaned_conn(
        self, instance_id: str, conn: ClientConnection, turn_id: str
    ) -> None:
        try:
            await asyncio.sleep(RECONNECT_APPROVAL_GRACE_SECONDS)
            if self._orphaned_conns.get(instance_id) is not conn:
                return
            self._orphaned_conns.pop(instance_id, None)
            for approval_id, (pending_turn, fut) in list(self._pending_approvals.items()):
                if pending_turn == turn_id and not fut.done():
                    fut.cancel()
            await self._cancel_active_turn(conn, notify=False)
            logger.warning(
                "Reconnect grace expired for instance=%s; cancelled turn=%s",
                instance_id,
                turn_id,
            )
        except asyncio.CancelledError:
            pass

    async def _maybe_resume_connection(
        self, conn: ClientConnection, payload: SystemHelloPayload
    ) -> ClientConnection:
        instance_id = (payload.client_instance_id or "").strip()
        conn.client_instance_id = instance_id
        if not instance_id:
            return conn
        old = self._orphaned_conns.pop(instance_id, None)
        if old is None:
            return conn

        if old.grace_task is not None and not old.grace_task.done():
            old.grace_task.cancel()
        old.grace_task = None
        old.websocket = conn.websocket
        old.disconnected = False

        # Keep the original ClientConnection object so the in-flight turn's
        # closure now writes to the new transport; alias the new transport id.
        self.clients.pop(conn.client_id, None)
        self.clients[old.client_id] = old
        self._conn_aliases[conn.client_id] = old
        logger.info(
            "Resumed connection instance=%s old_client=%s new_transport=%s",
            instance_id,
            old.client_id,
            conn.client_id,
        )
        return old

    async def _flush_outbox(self, conn: ClientConnection) -> None:
        if not conn.outbox:
            return
        pending = conn.outbox
        conn.outbox = []
        for envelope in pending:
            await conn.send_envelope(envelope)

    def _get_active_senses(self) -> Optional[ClientConnection]:
        for client in self.clients.values():
            if client.role == "senses":
                return client
        return None

    async def handle_raw_message(self, conn: ClientConnection, raw_text: str) -> None:
        resolved = self._resolve_conn(conn)
        if resolved is None:
            return
        conn = resolved
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
            conn = await self._maybe_resume_connection(conn, payload)
            conn.role = payload.role
            conn.client_name = payload.client_name
            conn.client_type = payload.client_type or (
                "body_generic" if payload.role == "body" else "senses"
            )
            conn.platform = payload.platform or (
                "macos"
                if conn.client_type == "body_macos"
                else ("windows" if conn.client_type == "body_windows" else "unknown")
            )
            conn.app_version = payload.app_version or ""
            conn.capabilities = payload.capabilities
            conn.ui_subscribed = bool(
                conn.client_type in ("body_macos", "body_windows")
                or conn.capabilities.native_approvals
                or conn.capabilities.native_desktop
                or conn.capabilities.mirror_turns
            )
            if payload.role == "senses":
                raw_payload = env.payload or {}
                self.senses_platform = str(raw_payload.get("platform", self.senses_platform))
                perms = raw_payload.get("permissions")
                if isinstance(perms, dict):
                    self.senses_permissions = PermissionStatus.model_validate(perms)
                self.senses_permission_hint = str(raw_payload.get("permission_hint", ""))
            logger.info(
                "Handshake hello: client=%s role=%s type=%s name=%s platform=%s audio_out=%s auto_hide=%s",
                conn.client_id,
                conn.role,
                conn.client_type,
                conn.client_name,
                conn.platform,
                conn.capabilities.audio_output,
                conn.capabilities.auto_hide,
            )
            await self._send_welcome(conn)
            await self._send_privacy_state(conn)
            if conn.ui_subscribed or (conn.role == "body" and self._pending_approval_requests):
                await self._send_ui_state(conn)
            if payload.role == "senses":
                await self._broadcast_privacy_state(exclude_client_id=conn.client_id)
            await self._flush_outbox(conn)
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
                    responder = conn.client_type or conn.client_name or conn.client_id
                    self._approval_responders[payload.approval_id] = (
                        responder,
                        payload.answer_text or "",
                    )
                    fut.set_result(payload)
            return

        if isinstance(payload, UISetStatePayload):
            conn.ui_subscribed = True
            await self._handle_ui_set_state(payload)
            return

        if isinstance(payload, UICommandPayload):
            await self._handle_ui_command(conn, payload)
            return

        if isinstance(payload, UICommandResultPayload):
            fut = self._ui_command_futures.pop(payload.command_id, None)
            if fut is not None and not fut.done():
                fut.set_result(payload)
            req_cid = self._command_requesters.pop(payload.command_id, None)
            if req_cid and req_cid in self.clients:
                try:
                    await self.clients[req_cid].send_envelope(
                        make_envelope("brain", "ui.command_result", payload)
                    )
                except Exception:
                    pass
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
            self._approved_tools_by_client.pop(self._approval_scope_key(conn), None)
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

        if payload.action == "dismiss":
            # Fold/unfold is client-local. Accept it silently instead of
            # synthesising a turn (the v1 behaviour was an LLM prompt).
            return

        turn_id = env.turn_id or new_turn_id()
        if payload.action == "ask_with_screen":
            user_text = str(payload.data.get("text") or "").strip() or "帮我看看我现在正在看的屏幕内容。"
            await self._start_turn(conn, turn_id, user_text, force_screen=True)
            return

        # Card actions must match a card the server just issued. This is both
        # the anti-forgery check and the reason unknown actions can no longer
        # be interpolated into a prompt.
        card_id = str(payload.card_id or "")
        allowed_actions = self._card_actions.get(card_id)
        if allowed_actions is None or payload.action not in allowed_actions:
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "system.error",
                    SystemErrorPayload(
                        code="invalid_card_action",
                        message="卡片动作已失效，请重新发起请求。",
                        recoverable=True,
                    ),
                    turn_id=turn_id,
                )
            )
            return

        if payload.action == "more_hint":
            prompt_text = "请基于刚才的防剧透提示，给出更进一步的第 2 级提示（依然尽量避免直接剧透结局）。"
        else:
            prompt_text = ""
        if not prompt_text:
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "system.error",
                    SystemErrorPayload(
                        code="unsupported_card_action",
                        message="该卡片动作暂不支持。",
                        recoverable=True,
                    ),
                    turn_id=turn_id,
                )
            )
            return
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

        # Capture this before cancelling: the task's finally block discards the
        # marker, but we must not emit a second turn_end for a turn that has
        # already completed (only its audio was still playing).
        end_already_sent = bool(old_turn_id and old_turn_id in self._turn_end_sent)
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

        if notify and old_turn_id and not end_already_sent:
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

    async def _send_shell_ui_action(
        self, action: str, args: Dict[str, Any]
    ) -> tuple[bool, str]:
        """Send a ui.command to the native shell and wait briefly for its ack.

        Used by Brain-side tools (e.g. opening the workspace) so the assistant
        can drive its own UI without going through the filesystem.
        """
        candidates = [
            c
            for c in self.clients.values()
            if c.role == "body" and c.capabilities.native_desktop
        ]
        if not candidates:
            return False, "shell_offline"
        target = candidates[0]

        command = UICommandPayload(
            command_id=f"ui_{uuid.uuid4().hex[:12]}",
            target="shell",
            action=action,
            args=args,
        )
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[UICommandResultPayload] = loop.create_future()
        self._ui_command_futures[command.command_id] = fut
        try:
            await target.send_envelope(make_envelope("brain", "ui.command", command))
        except Exception:
            self._ui_command_futures.pop(command.command_id, None)
            return False, "target_send_failed"

        try:
            result = await asyncio.wait_for(fut, timeout=2.0)
        except asyncio.TimeoutError:
            self._ui_command_futures.pop(command.command_id, None)
            return True, ""
        if result.ok:
            return True, ""
        return False, result.error or "ui_command_failed"

    async def _handle_ui_command(
        self, conn: ClientConnection, payload: UICommandPayload
    ) -> None:
        if payload.action == "open_path":
            raw_path = str((payload.args or {}).get("path") or "").strip()
            # Resolve symlinks before checking the extension: a benign-looking
            # .md link must not be able to point at a .app/.command payload.
            resolved_path = os.path.realpath(os.path.expanduser(raw_path))
            suffix = Path(resolved_path).suffix.lower()
            if suffix in _DANGEROUS_OPEN_EXTENSIONS:
                await conn.send_envelope(
                    make_envelope(
                        "brain",
                        "ui.command_result",
                        UICommandResultPayload(
                            command_id=payload.command_id,
                            ok=False,
                            error="dangerous_executable_blocked",
                            data={"path": resolved_path, "suffix": suffix},
                        ),
                    )
                )
                return

        if payload.target == "all" and payload.action == "sync_state":
            conn.ui_subscribed = True
            await self._broadcast_ui_state()
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "ui.command_result",
                    UICommandResultPayload(command_id=payload.command_id, ok=True),
                )
            )
            return

        target_conn: Optional[ClientConnection] = None
        if payload.target == "avatar":
            for c in self.clients.values():
                if c.role == "body" and c.client_type == "body_godot":
                    target_conn = c
                    break
        elif payload.target == "shell":
            candidates = [
                c
                for c in self.clients.values()
                if c.role == "body" and c.capabilities.native_desktop
            ]
            if conn.platform != "unknown":
                for c in candidates:
                    if c.platform == conn.platform:
                        target_conn = c
                        break
            if target_conn is None and candidates:
                target_conn = candidates[0]
        elif payload.target == "senses":
            target_conn = self._get_active_senses()

        if target_conn is None:
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "ui.command_result",
                    UICommandResultPayload(
                        command_id=payload.command_id,
                        ok=False,
                        error="target_offline",
                    ),
                )
            )
            return

        self._command_requesters[payload.command_id] = conn.client_id
        try:
            await target_conn.send_envelope(make_envelope("brain", "ui.command", payload))
        except Exception:
            self._command_requesters.pop(payload.command_id, None)
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "ui.command_result",
                    UICommandResultPayload(
                        command_id=payload.command_id,
                        ok=False,
                        error="target_send_failed",
                    ),
                )
            )

    def _get_primary_body_conn(
        self, fallback_conn: Optional[ClientConnection] = None
    ) -> Optional[ClientConnection]:
        bodies = [c for c in self.clients.values() if c.role == "body"]
        # The native macOS shell is the product primary surface. Godot is an
        # optional decorative plugin and must never steal primary routing.
        for c in bodies:
            if c.client_type == "body_macos":
                return c
        for c in bodies:
            if c.client_type != "body_godot" and c.capabilities.native_desktop:
                return c
        for c in bodies:
            if c.client_type == "body_godot" or c.capabilities.audio_output:
                return c
        if bodies:
            return bodies[0]
        return fallback_conn

    def _get_tts_target_conns(self, primary_conn: ClientConnection) -> List[ClientConnection]:
        godot_audio = [
            c
            for c in self.clients.values()
            if c.role == "body" and c.client_type == "body_godot" and c.capabilities.audio_output
        ]
        if godot_audio:
            return godot_audio
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
        if hasattr(self.asr_provider, "update_config"):
            self.asr_provider.update_config(fresh_cfg.audio.asr)  # type: ignore[attr-defined]
        elif hasattr(self.asr_provider, "config"):
            self.asr_provider.config = fresh_cfg.audio.asr  # type: ignore[attr-defined]
        if hasattr(self.tts_provider, "update_config"):
            self.tts_provider.update_config(fresh_cfg.audio.tts)  # type: ignore[attr-defined]
        elif hasattr(self.tts_provider, "config"):
            self.tts_provider.config = fresh_cfg.audio.tts  # type: ignore[attr-defined]
        if hasattr(self.screen_router, "reload_config"):
            self.screen_router.reload_config(fresh_cfg)
        elif hasattr(self.screen_router, "ocr_provider") and hasattr(
            self.screen_router.ocr_provider, "config"
        ):
            self.screen_router.ocr_provider.config = fresh_cfg.ocr  # type: ignore[attr-defined]
        inbox_tool = self.tool_registry.get("inbox_scan")
        if inbox_tool is not None and hasattr(inbox_tool, "config"):
            inbox_tool.config = fresh_cfg  # type: ignore[attr-defined]
        image_tool = self.tool_registry.get("web_image_search")
        if fresh_cfg.tools.image_search.enabled:
            if image_tool is not None and hasattr(image_tool, "update_config"):
                image_tool.update_config(fresh_cfg.tools.image_search)  # type: ignore[attr-defined]
            elif image_tool is None:
                from brain.tools.image_search import ImageSearchTool

                self.tool_registry.register(ImageSearchTool(fresh_cfg.tools.image_search))
        elif image_tool is not None:
            self.tool_registry.unregister("web_image_search")
            close = getattr(image_tool, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    logger.debug("Failed closing disabled image search tool", exc_info=True)
        self.router_include_mcp = False
        self.router_tool_registry = build_router_registry(
            self.tool_registry,
            extra_tool_names=self.router_extra_tool_names,
        )
        await self._broadcast_welcome_to_bodies()
        await self._broadcast_privacy_state()
        await self._broadcast_shortcuts_sync()
        await self._broadcast_ui_state()

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
        resolved = self._resolve_conn(conn)
        if resolved is None:
            return
        conn = resolved
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
                        provider=self.config.audio.asr.provider,
                        slot="asr",
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
                                    provider=self.config.audio.tts.provider,
                                    slot="tts",
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

    async def broadcast_task_status(self, payload: TaskStatusPayload) -> None:
        env = make_envelope("brain", "task.status", payload, turn_id=payload.task_id)
        for client in list(self.clients.values()):
            if client.role in ("body", None):
                try:
                    await client.send_envelope(env)
                except Exception:
                    logger.debug("Failed broadcasting task.status to %s", client.client_id)

    async def _broadcast_to_bodies(self, envelope: Any, label: str) -> None:
        for client in list(self.clients.values()):
            if client.role in ("body", None):
                try:
                    await client.send_envelope(envelope)
                except Exception:
                    logger.debug("Failed broadcasting %s to %s", label, client.client_id)

    async def broadcast_task_state_changed(self, payload: TaskStateChangedPayload) -> None:
        env = make_envelope(
            "brain", "task.state_changed", payload, turn_id=payload.task_id
        )
        await self._broadcast_to_bodies(env, "task.state_changed")

    async def broadcast_task_followup_acked(self, payload: TaskFollowupAckedPayload) -> None:
        env = make_envelope(
            "brain", "task.followup_acked", payload, turn_id=payload.task_id
        )
        await self._broadcast_to_bodies(env, "task.followup_acked")

    async def broadcast_task_watchdog_alert(self, payload: TaskWatchdogAlertPayload) -> None:
        env = make_envelope(
            "brain", "task.watchdog_alert", payload, turn_id=payload.task_id
        )
        await self._broadcast_to_bodies(env, "task.watchdog_alert")

    async def broadcast_artifact_capsule(
        self, payload: OutputArtifactCapsulePayload
    ) -> None:
        env = make_envelope(
            "brain", "output.artifact_capsule", payload, turn_id=payload.turn_id
        )
        for client in list(self.clients.values()):
            if client.role in ("body", None):
                try:
                    await client.send_envelope(env)
                except Exception:
                    logger.debug("Failed broadcasting artifact_capsule to %s", client.client_id)

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
            cached = int(u.cached_input_tokens or u.prompt_cache_hit_tokens or 0)
            cached = max(0, min(int(u.prompt_tokens or 0), cached))
            rate = (cached / u.prompt_tokens) if u.prompt_tokens > 0 else 0.0
            logger.debug(
                "usage turn=%s provider=%s slot=%s model=%s cache_hit=%d/%d (%.1f%%)",
                turn_id,
                u.provider,
                u.slot,
                u.model,
                cached,
                u.prompt_tokens,
                rate * 100.0,
            )
            await self.memory_backend.add_usage_record(
                UsageLedgerRecord(
                    session_id=session_id,
                    turn_id=turn_id,
                    profile=u.profile,
                    provider=u.provider,
                    slot=u.slot,
                    model=u.model,
                    prompt_tokens=u.prompt_tokens,
                    prompt_cache_hit_tokens=u.prompt_cache_hit_tokens,
                    cached_input_tokens=cached,
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
