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


def build_turn_tools_schema(
    registry: Any,
    config: AppConfig,
    profile: Any,
    *,
    use_tools: bool,
) -> tuple[List[Dict[str, Any]], bool]:
    """Return (tools schema, native_web_tools_enabled) for one turn."""
    schema = registry.to_openai_tools() if use_tools else []
    native_web = bool(
        use_tools
        and config.deepseek.native_web_tools
        and config.tools.web_search.enabled
        and (profile.model or "").lower() == "deepseek-flash"
        and (
            (profile.provider_id or "").lower() == "deepseek"
            or "api.deepseek.com" in (profile.base_url or "").lower()
        )
    )
    if not native_web:
        return schema, False
    schema = [
        tool
        for tool in schema
        if (tool.get("function") or {}).get("name") not in ("web_search", "web_fetch")
    ]
    schema.extend(
        [
            {"type": "deepseek_web_search"},
            {"type": "deepseek_read_url"},
        ]
    )
    return schema, True

SERVER_VERSION = get_app_version()
CAPTURE_TIMEOUT_SECONDS = 3.5
_MAX_TTS_CHARS_PER_TURN = 600
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


@dataclass
class ClientConnection:
    client_id: str
    websocket: WebSocket
    role: Optional[SourceRole] = None
    client_name: str = "unknown"
    client_type: Optional[str] = None
    platform: str = "unknown"
    app_version: str = ""
    client_instance_id: str = ""
    ui_subscribed: bool = False
    capabilities: ClientCapabilities = field(default_factory=ClientCapabilities)
    active_turn_id: Optional[str] = None
    active_task: Optional[asyncio.Task[None]] = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    disconnected: bool = False
    outbox: List[Envelope] = field(default_factory=list)
    grace_task: Optional[asyncio.Task[None]] = None

    async def send_envelope(self, envelope: Envelope) -> None:
        async with self.send_lock:
            if self.disconnected:
                self._queue_outbox(envelope)
                return
            try:
                await self.websocket.send_text(envelope.model_dump_json())
            except Exception:
                # Keep the turn alive during a short reconnect window; the
                # envelope is replayed after the same client instance resumes.
                self.disconnected = True
                self._queue_outbox(envelope)

    def _queue_outbox(self, envelope: Envelope) -> None:
        self.outbox.append(envelope)
        if len(self.outbox) > 512:
            self.outbox = self.outbox[-512:]


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

    def _has_pending_approval_for_turn(self, turn_id: str) -> bool:
        return any(
            req.turn_id == turn_id for req in self._pending_approval_requests.values()
        )

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

    @staticmethod
    def _approval_scope_key(conn: ClientConnection) -> str:
        return conn.client_name or conn.client_id

    def _approval_scope_set(self, conn: ClientConnection) -> set[str]:
        return self._approved_tools_by_client.setdefault(
            self._approval_scope_key(conn), set()
        )

    def _register_card_actions(self, card_id: str, action_ids: List[str]) -> None:
        if not card_id:
            return
        self._card_actions[card_id] = set(action_ids)
        while len(self._card_actions) > self._card_action_limit:
            oldest = next(iter(self._card_actions))
            self._card_actions.pop(oldest, None)

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

    def _build_ui_state_payload(self) -> UIStatePayload:
        return UIStatePayload(
            avatar_enabled=self.config.ui.avatar_enabled,
            avatar_scale=self.config.ui.avatar_scale,
            roam=self.config.ui.roam,
            earcons=self.config.ui.earcons,
            ambient=self.config.ui.ambient,
            docked=False,
            active_profile=self.llm_router.active_profile,
            privacy_mode=self.privacy_mode,
            shortcuts=self._build_shortcuts_sync_payload(),
            pending_approvals=list(self._pending_approval_requests.values()),
        )

    async def _send_ui_state(self, conn: ClientConnection) -> None:
        await conn.send_envelope(
            make_envelope("brain", "ui.state", self._build_ui_state_payload())
        )

    async def _broadcast_ui_state(self) -> None:
        env = make_envelope("brain", "ui.state", self._build_ui_state_payload())
        for client in list(self.clients.values()):
            if client.role in ("body", None) and client.ui_subscribed:
                try:
                    await client.send_envelope(env)
                except Exception:
                    logger.debug("Failed broadcasting ui.state to %s", client.client_id)

    async def _handle_ui_set_state(self, payload: UISetStatePayload) -> None:
        patch = payload.patch or {}
        if "avatar_enabled" in patch:
            self.config.ui.avatar_enabled = bool(patch["avatar_enabled"])
        if "avatar_scale" in patch and patch["avatar_scale"] is not None:
            try:
                val = float(patch["avatar_scale"])
                self.config.ui.avatar_scale = max(0.5, min(1.5, val))
            except (TypeError, ValueError):
                pass
        if "roam" in patch:
            self.config.ui.roam = bool(patch["roam"])
        if "earcons" in patch:
            self.config.ui.earcons = bool(patch["earcons"])
            self.config.godot.earcons_enabled = bool(patch["earcons"])
        if "ambient" in patch:
            self.config.ui.ambient = bool(patch["ambient"])
        await self._broadcast_ui_state()

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

    async def _send_with_mirrors(
        self, origin_conn: ClientConnection, envelope: Envelope
    ) -> None:
        await origin_conn.send_envelope(envelope)
        for client in list(self.clients.values()):
            if (
                client.client_id != origin_conn.client_id
                and client.role == "body"
                and client.capabilities.mirror_turns
            ):
                try:
                    await client.send_envelope(envelope)
                except Exception:
                    pass

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

        def _log_turn_crash(t: asyncio.Task[None]) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.error(
                    "Turn task crashed turn_id=%s: %s",
                    turn_id,
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(_log_turn_crash)
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

    async def _broadcast_approval_request(
        self,
        req: ToolApprovalRequestPayload,
        *,
        origin_conn: Optional[ClientConnection],
        broadcast_all_bodies: bool = False,
    ) -> None:
        env = make_envelope("brain", "tool.approval_request", req, turn_id=req.turn_id)
        sent_ids: set[str] = set()
        if origin_conn is not None:
            await origin_conn.send_envelope(env)
            sent_ids.add(origin_conn.client_id)
        for client in list(self.clients.values()):
            if client.client_id in sent_ids or client.role != "body":
                continue
            if broadcast_all_bodies or client.capabilities.native_approvals:
                try:
                    await client.send_envelope(env)
                    sent_ids.add(client.client_id)
                except Exception:
                    logger.debug("Failed sending approval_request to %s", client.client_id)

    async def _broadcast_approval_resolved(
        self,
        *,
        approval_id: str,
        turn_id: str,
        approved: bool,
        answered_by: str,
        answer_text: str = "",
    ) -> None:
        env = make_envelope(
            "brain",
            "tool.approval_resolved",
            ToolApprovalResolvedPayload(
                approval_id=approval_id,
                turn_id=turn_id,
                approved=approved,
                answered_by=answered_by,
                answer_text=answer_text,
            ),
            turn_id=turn_id,
        )
        for client in list(self.clients.values()):
            if client.role in ("body", None):
                try:
                    await client.send_envelope(env)
                except Exception:
                    logger.debug("Failed broadcasting approval_resolved to %s", client.client_id)

    async def _request_tool_approval(
        self,
        *,
        conn: Optional[ClientConnection],
        turn_id: str,
        tool_name: str,
        risk_level: RiskLevel,
        summary: str,
        details: Dict[str, Any],
        timeout_sec: int,
        broadcast_all_bodies: bool = False,
    ) -> tuple[bool, bool]:
        approval_id = f"apr_{uuid.uuid4().hex[:10]}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ToolApprovalResponsePayload] = loop.create_future()
        req = ToolApprovalRequestPayload(
            approval_id=approval_id,
            turn_id=turn_id,
            tool_name=tool_name,
            risk_level=risk_level,
            summary=summary,
            details=details,
            timeout_sec=timeout_sec,
        )
        self._pending_approvals[approval_id] = (turn_id, fut)
        self._pending_approval_requests[approval_id] = req
        approved = False
        answered_by = "timeout"
        answer_text = ""
        try:
            await self._broadcast_approval_request(
                req, origin_conn=conn, broadcast_all_bodies=broadcast_all_bodies
            )
            resp = await asyncio.wait_for(fut, timeout=max(1, timeout_sec))
            approved = bool(resp.approved)
            answered_by, answer_text = self._approval_responders.pop(
                approval_id, ("body", resp.answer_text or "")
            )
            return approved, bool(resp.remember_for_session)
        except asyncio.TimeoutError:
            logger.info("Tool approval timed out approval_id=%s tool=%s", approval_id, tool_name)
            return False, False
        except asyncio.CancelledError:
            answered_by = "cancelled"
            raise
        finally:
            self._pending_approvals.pop(approval_id, None)
            self._pending_approval_requests.pop(approval_id, None)
            self._approval_responders.pop(approval_id, None)
            await self._broadcast_approval_resolved(
                approval_id=approval_id,
                turn_id=turn_id,
                approved=approved,
                answered_by=answered_by,
                answer_text=answer_text,
            )

    async def _request_user_clarification(
        self,
        *,
        conn: Optional[ClientConnection],
        turn_id: str,
        question: str,
        options: List[str],
        timeout_sec: int,
        broadcast_all_bodies: bool = False,
    ) -> Optional[str]:
        approval_id = f"ask_{uuid.uuid4().hex[:10]}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ToolApprovalResponsePayload] = loop.create_future()
        clean_options = [str(o).strip() for o in options if str(o).strip()][:4]
        req = ToolApprovalRequestPayload(
            approval_id=approval_id,
            turn_id=turn_id,
            tool_name="ask_user",
            risk_level="safe",
            summary=question,
            details={
                "kind": "clarification",
                "question": question,
                "options": clean_options,
                "allow_remember": False,
            },
            timeout_sec=timeout_sec,
        )
        self._pending_approvals[approval_id] = (turn_id, fut)
        self._pending_approval_requests[approval_id] = req
        approved = False
        answered_by = "timeout"
        answer_text = ""
        try:
            await self._broadcast_approval_request(
                req, origin_conn=conn, broadcast_all_bodies=broadcast_all_bodies
            )
            resp = await asyncio.wait_for(fut, timeout=max(1, timeout_sec))
            ans = (resp.answer_text or "").strip()
            approved = bool(resp.approved and ans)
            answered_by, _ = self._approval_responders.pop(approval_id, ("body", ans))
            answer_text = ans
            if approved:
                return ans
            return None
        except asyncio.TimeoutError:
            logger.info("Clarification request timed out approval_id=%s", approval_id)
            return None
        except asyncio.CancelledError:
            answered_by = "cancelled"
            raise
        finally:
            self._pending_approvals.pop(approval_id, None)
            self._pending_approval_requests.pop(approval_id, None)
            self._approval_responders.pop(approval_id, None)
            await self._broadcast_approval_resolved(
                approval_id=approval_id,
                turn_id=turn_id,
                approved=approved,
                answered_by=answered_by,
                answer_text=answer_text,
            )

    async def request_background_approval(
        self,
        task_id: str,
        tool_name: str,
        risk_level: RiskLevel,
        summary: str,
        details: Dict[str, Any],
        timeout_sec: int,
    ) -> tuple[bool, bool]:
        return await self._request_tool_approval(
            conn=None,
            turn_id=task_id,
            tool_name=tool_name,
            risk_level=risk_level,
            summary=summary,
            details=details,
            timeout_sec=timeout_sec,
            broadcast_all_bodies=True,
        )

    async def request_background_clarification(
        self,
        task_id: str,
        question: str,
        options: List[str],
        timeout_sec: int,
    ) -> Optional[str]:
        return await self._request_user_clarification(
            conn=None,
            turn_id=task_id,
            question=question,
            options=options,
            timeout_sec=timeout_sec,
            broadcast_all_bodies=True,
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
        parser = StreamParser()
        seq = 0
        collected_spoken: List[str] = []
        collected_cards: List[str] = []
        collected_card_payloads: List[Dict[str, Any]] = []
        attachment_names: List[str] = []
        artifact_id_to_burn = ""
        artifacts_to_burn: List[str] = list(attachments or [])
        extra_l3_sections: List[str] = []
        cancel_event = asyncio.Event()
        self._turn_cancel_events[turn_id] = cancel_event

        # Coalesce text deltas into ~40ms frames. Upstream providers emit many
        # tiny chunks; one WS frame + one SwiftUI publish per chunk is wasteful.
        text_send_buffer: List[str] = []
        text_flush_task: Optional[asyncio.Task[None]] = None
        text_flush_event = asyncio.Event()
        last_text_flush_at = 0.0

        async def _flush_text_delta_buffer() -> None:
            nonlocal seq, last_text_flush_at
            if not text_send_buffer:
                return
            delta = "".join(text_send_buffer)
            text_send_buffer.clear()
            seq += 1
            last_text_flush_at = time.monotonic()
            await self._send_with_mirrors(
                conn,
                make_envelope(
                    "brain",
                    "output.text_delta",
                    OutputTextDeltaPayload(seq=seq, delta=delta, is_final=False),
                    turn_id=turn_id,
                ),
            )

        async def _flush_text_delta_soon() -> None:
            nonlocal text_flush_task
            try:
                while text_send_buffer:
                    try:
                        await asyncio.wait_for(text_flush_event.wait(), timeout=0.04)
                    except asyncio.TimeoutError:
                        pass
                    text_flush_event.clear()
                    await _flush_text_delta_buffer()
            finally:
                text_flush_task = None
                text_flush_event.clear()

        async def _queue_text_delta(delta: str) -> None:
            nonlocal text_flush_task
            if not delta:
                return
            text_send_buffer.append(delta)
            if text_flush_task is None:
                if len(text_send_buffer) == 1 and (
                    time.monotonic() - last_text_flush_at >= 0.04
                ):
                    await _flush_text_delta_buffer()
                else:
                    text_flush_task = asyncio.create_task(_flush_text_delta_soon())

        async def _drain_text_delta_buffer() -> None:
            nonlocal text_flush_task
            task = text_flush_task
            if task is not None and not task.done():
                # Wake the 40ms sleeper immediately instead of waiting for the
                # timer to expire at the end of a turn.
                text_flush_event.set()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            text_flush_task = None
            await _flush_text_delta_buffer()

        try:
            # Resolve the profile inside the try: a missing/invalid model must
            # still produce system.error + output.turn_end instead of escaping
            # the coroutine and leaving the shell stuck in "generating".
            profile = self.llm_router.get_profile()
            await self._send_with_mirrors(
                conn,
                make_envelope(
                    "brain",
                    "output.turn_start",
                    OutputTurnStartPayload(
                        profile=profile.name,
                        provider=profile.provider,
                        model=profile.model,
                        origin_client_id=conn.client_id,
                        origin_client_type=conn.client_type,
                        user_text=user_text,
                    ),
                    turn_id=turn_id,
                ),
            )
            await self._send_with_mirrors(
                conn,
                make_envelope(
                    "brain",
                    "avatar.state",
                    AvatarStatePayload(emotion="thinking", intensity=0.7, activity="thinking"),
                    turn_id=turn_id,
                ),
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
                    attachment_names.append(str(rec.filename or ""))
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
                        suffix = Path(rec.filename).suffix.lower()
                        if suffix in {".pdf", ".xlsx", ".xlsm", ".csv", ".docx", ".md", ".markdown", ".txt"}:
                            try:
                                from brain.doc_office import inspect_document, inspect_text_summary

                                inspected = await asyncio.to_thread(
                                    inspect_document, rec.path, max_rows=20
                                )
                                extra_l3_sections.append(
                                    f"【用户直投文件附件 ({rec.filename})，本地路径: {rec.path}】\n"
                                    + inspect_text_summary(inspected, max_chars=6000)
                                )
                                continue
                            except Exception:
                                pass
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

            # Router-only capability routing context. Insert first so file OCR
            # or long attachment previews cannot truncate it away.
            extra_l3_sections.insert(0, build_capability_catalog())
            intent_hint = build_intent_hint(user_text, attachments=attachment_names)
            if intent_hint:
                extra_l3_sections.insert(1, intent_hint)

            # L3 dynamic tail: a deliberately tiny active-task table. It must
            # never enter the stable L0/L1 prefix; details stay behind
            # task_inspect(task_id, ref_id).
            if self.task_runner is not None:
                try:
                    live_tasks = await self.task_runner.list_tasks(limit=8)
                    live = [
                        t for t in live_tasks
                        if t.status in ("queued", "planning", "running", "waiting_approval", "waiting_input", "verifying", "stalled")
                    ]
                    if live:
                        lines = ["【当前后台任务（仅摘要）】"]
                        for t in live[:5]:
                            step = (t.current_step or "").replace("\n", " ")[:60]
                            lines.append(
                                f"- {t.task_id} | {t.status} | {t.title[:40]} | {step}"
                            )
                        lines.append("需要细节时调用 task_inspect；追加要求调用 task_followup。")
                        extra_l3_sections.append("\n".join(lines)[:700])
                except Exception:
                    logger.debug("Failed building active task L3 table", exc_info=True)

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
                    limit=100,
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

            self.router_tool_registry = build_router_registry(
                self.tool_registry,
                include_mcp=self.router_include_mcp,
                extra_tool_names=self.router_extra_tool_names,
            )
            use_tools = bool(
                self.config.tools.enabled
                and len(self.router_tool_registry) > 0
                and (
                    profile.provider != "mock"
                    or type(self.llm_router).complete_with_tools is not LLMRouter.complete_with_tools
                )
            )
            tools_schema, native_web_tools = build_turn_tools_schema(
                self.router_tool_registry,
                self.config,
                profile,
                use_tools=use_tools,
            )
            resolved_budget = resolve_context_budget(
                profile=profile,
                config=self.config,
                system_prompt=BASE_SYSTEM_PROMPT,
                tools_schema=tools_schema,
                has_facts=bool(core_facts),
                has_summary=bool(rolling_summary.strip()),
            )
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
                resolved_budget=resolved_budget,
                tools_tokens=estimate_tools_tokens(tools_schema),
            )
            self.last_build_stats = built.stats
            messages = built.messages

            turn_usages: List[LLMUsage] = []
            trace_steps: List[ToolTraceStep] = []
            loop_sources: List[Dict[str, Any]] = []
            tool_sources: List[Dict[str, Any]] = []
            tool_media_items: List[Dict[str, Any]] = []
            media_paths: List[str] = []

            tts_targets = (
                self._get_tts_target_conns(conn)
                if self.config.audio.enabled and self.config.audio.tts.enabled
                else []
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

            if use_tools:
                async def _on_text_delta(chunk: str) -> None:
                    events = parser.feed(chunk)
                    await self._emit_parser_events(
                        conn, turn_id, events, seq, collected_spoken, on_spoken_delta=_enqueue_tts_delta,
                        collected_cards=collected_cards,
                        collected_card_payloads=collected_card_payloads,
                        on_text_delta=_queue_text_delta,
                        flush_pending_text=_drain_text_delta_buffer,
                    )

                async def _on_status(
                    status: str,
                    tool_name: str,
                    label: str,
                    step: Optional[int],
                    max_steps: Optional[int],
                ) -> None:
                    # Keep textual order: tool status must not overtake text
                    # that is still sitting in the 40ms coalescing buffer.
                    await _drain_text_delta_buffer()
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

                async def _on_server_tool_event(event: Dict[str, Any]) -> None:
                    etype = str(event.get("type") or "")
                    call = event.get("call") or {}
                    if not isinstance(call, dict):
                        call = {}
                    kind = str(call.get("kind") or "")
                    tool_name = (
                        "deepseek_read_url"
                        if kind == "read_link"
                        else "deepseek_web_search"
                    )
                    if etype == "call_started":
                        if tool_name == "deepseek_read_url":
                            label = "正在阅读网页…"
                        elif kind == "deep_search":
                            label = "正在深度联网搜索…"
                        else:
                            label = "正在联网搜索…"
                        await _on_status("running", tool_name, label, None, None)
                    elif etype == "call_completed":
                        ok = str(call.get("status") or "completed") == "completed"
                        await _on_status(
                            "completed" if ok else "error",
                            tool_name,
                            "网页阅读完成" if ok and tool_name == "deepseek_read_url"
                            else ("联网搜索完成" if ok else "联网搜索失败"),
                            None,
                            None,
                        )

                async def _on_approval(
                    tool_name: str,
                    risk_level: RiskLevel,
                    summary: str,
                    details: Dict[str, Any],
                    timeout_sec: int,
                ) -> tuple[bool, bool]:
                    await _drain_text_delta_buffer()
                    return await self._request_tool_approval(
                        conn=conn,
                        turn_id=turn_id,
                        tool_name=tool_name,
                        risk_level=risk_level,
                        summary=summary,
                        details=details,
                        timeout_sec=timeout_sec,
                    )

                async def _on_ui_command(
                    action: str,
                    args: Dict[str, Any],
                ) -> tuple[bool, str]:
                    await _drain_text_delta_buffer()
                    return await self._send_shell_ui_action(action, args)

                async def _on_ask_user(
                    question: str,
                    options: List[str],
                    timeout_sec: int,
                ) -> Optional[str]:
                    await _drain_text_delta_buffer()
                    return await self._request_user_clarification(
                        conn=conn,
                        turn_id=turn_id,
                        question=question,
                        options=options,
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
                    session_approved_tools=self._approval_scope_set(conn),
                    on_status=_on_status,
                    on_approval=_on_approval,
                    on_ask_user=_on_ask_user,
                    on_ui_command=_on_ui_command,
                    task_runner=self.task_runner,
                )
                loop_res = await self.tool_executor.run(
                    messages=messages,
                    registry=self.router_tool_registry,
                    ctx=tool_ctx,
                    profile_name=profile.name,
                    on_text_delta=_on_text_delta,
                    max_context_tokens=(
                        built.stats.total_tokens + resolved_budget.tool_loop_max
                    ),
                    tools_schema=tools_schema,
                    on_server_tool_event=_on_server_tool_event,
                )
                trace_steps = loop_res.trace_steps
                turn_usages = loop_res.usages
                loop_sources = list(loop_res.sources)
                if loop_res.usages:
                    self.context_builder.observe_prompt_usage(
                        built.stats.total_with_tools,
                        int(loop_res.usages[0].prompt_tokens),
                        has_images=built.stats.has_image_input,
                    )
            else:
                async for chunk in self.llm_router.stream_chat(messages, profile_name=profile.name):
                    events = parser.feed(chunk)
                    await self._emit_parser_events(
                        conn, turn_id, events, seq, collected_spoken, on_spoken_delta=_enqueue_tts_delta,
                        collected_cards=collected_cards,
                        collected_card_payloads=collected_card_payloads,
                        on_text_delta=_queue_text_delta,
                        flush_pending_text=_drain_text_delta_buffer,
                    )
                if self.llm_router.last_usage is not None:
                    turn_usages.append(self.llm_router.last_usage)
                    self.context_builder.observe_prompt_usage(
                        built.stats.total_with_tools,
                        int(self.llm_router.last_usage.prompt_tokens),
                        has_images=built.stats.has_image_input,
                    )

            vision_usage = getattr(self.screen_router, "last_ocr_usage", None)
            if vision_usage is not None:
                turn_usages = [vision_usage, *turn_usages]

            tail_events = parser.finish()
            await self._emit_parser_events(
                conn, turn_id, tail_events, seq, collected_spoken, on_spoken_delta=_enqueue_tts_delta,
                collected_cards=collected_cards,
                collected_card_payloads=collected_card_payloads,
                on_text_delta=_queue_text_delta,
                flush_pending_text=_drain_text_delta_buffer,
            )

            if chunker is not None and tts_queue is not None:
                for sent in chunker.flush():
                    if tts_chars_sent >= _MAX_TTS_CHARS_PER_TURN:
                        break
                    tts_queue.put_nowait(sent)
                    tts_chars_sent += len(sent)
                tts_queue.put_nowait(None)

            for s in trace_steps:
                media_paths.extend(s.media_paths)
                tool_media_items.extend(s.media_items)
                tool_sources.extend(s.sources)
                raw_out_path = str(
                    s.arguments.get("path")
                    or s.arguments.get("output_path")
                    or s.arguments.get("target_path")
                    or ""
                ).strip()
                if raw_out_path and os.path.isfile(os.path.expanduser(raw_out_path)):
                    media_paths.append(raw_out_path)
                    if s.status == "ok" and any(
                        k in s.tool_name.lower()
                        for k in ("write", "create", "save", "export", "generate")
                    ):
                        try:
                            await self.emit_artifact_capsule(
                                conn, turn_id=turn_id, path=raw_out_path
                            )
                        except Exception:
                            pass

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            raw_assistant_reply = "".join(collected_spoken).strip()
            card_markdown = "\n\n".join(
                str(item.get("content_markdown") or "").strip()
                for item in collected_card_payloads
                if str(item.get("content_markdown") or "").strip()
            )
            # Tables may be emitted inside a <<<CARD>>> (the system prompt
            # encourages cards for long content). Cards are streamed as a
            # separate event, so scan their markdown too or the table would
            # silently disappear from the shell.
            table_source = "\n\n".join(
                [raw_assistant_reply] + [text for text in collected_cards if text.strip()]
            ).strip()
            table_media_payloads = extract_table_media(
                table_source, turn_id=turn_id
            )
            base_sources = loop_sources if use_tools else self.llm_router.last_annotations
            raw_sources = list(base_sources) + tool_sources
            assistant_reply, source_payloads = clean_answer_and_sources(
                raw_assistant_reply, raw_sources
            )
            if source_payloads and needs_source_labeling(source_payloads):
                fast_profile = self.config.resolve_slot_profile("fast_utility")
                use_tagger = not (
                    profile.provider == "mock"
                    and fast_profile.provider == "mock"
                    and type(self.llm_router).stream_chat is LLMRouter.stream_chat
                )
                source_payloads = await label_sources_with_llm(
                    source_payloads,
                    context_text=table_source,
                    snippets_by_url=extract_source_snippets(raw_sources),
                    llm_router=self.llm_router if use_tagger else None,
                    profile_name=fast_profile.name or profile.name,
                )
            media_payloads = collect_media(media_paths)
            if tool_media_items:
                by_path = {
                    str(item.get("local_path") or ""): item
                    for item in media_payloads
                    if item.get("local_path")
                }
                for item in tool_media_items:
                    path = str(item.get("local_path") or "")
                    if path:
                        by_path[path] = item
                media_payloads = list(by_path.values())
            if table_media_payloads:
                table_ids = {item["media_id"] for item in table_media_payloads}
                media_payloads = table_media_payloads + [
                    item for item in media_payloads
                    if item.get("media_id") not in table_ids
                ]
            media_payloads = media_payloads[:20]
            turn_sources = [SourceItem(**item) for item in source_payloads]
            turn_media = [MediaItem(**item) for item in media_payloads]
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
                # A card-only answer has no spoken text; store the card body as
                # the assistant content so the turn is not lost from memory.
                stored_assistant_content = assistant_reply or card_markdown
                if stored_assistant_content or collected_card_payloads:
                    assistant_rec = await self.memory_backend.append_message(
                        session_id=active_session_id,
                        turn_id=turn_id,
                        role="assistant",
                        content=stored_assistant_content,
                        desktop_context_summary=scene_summary,
                        sources=source_payloads,
                        media=media_payloads,
                        cards=collected_card_payloads,
                    )
                if self.memory_extractor is not None:
                    self.memory_extractor.schedule_post_turn(
                        session_id=active_session_id,
                        user_msg=user_rec,
                        assistant_msg=assistant_rec,
                        context_store=self.context_store,
                        recent_budget=built.stats.recent_budget,
                    )
            else:
                self.memory_store.append_turn(
                    conn.client_id, "user", user_text, scene_summary=scene_summary
                )
                if assistant_reply or card_markdown:
                    self.memory_store.append_turn(
                        conn.client_id,
                        "assistant",
                        assistant_reply or card_markdown,
                        scene_summary=scene_summary,
                    )

            try:
                await _drain_text_delta_buffer()
            except Exception:
                logger.debug(
                    "Failed flushing pending text deltas", exc_info=True
                )
            await self._send_with_mirrors(
                conn,
                make_envelope(
                    "brain",
                    "output.text_delta",
                    OutputTextDeltaPayload(seq=seq + 1, delta="", is_final=True),
                    turn_id=turn_id,
                ),
            )
            self._turn_end_sent.add(turn_id)
            await self._send_with_mirrors(
                conn,
                make_envelope(
                    "brain",
                    "output.turn_end",
                    OutputTurnEndPayload(
                        finish_reason="completed",
                        elapsed_ms=elapsed_ms,
                        sources=turn_sources,
                        media=turn_media,
                    ),
                    turn_id=turn_id,
                ),
            )
            await self._send_with_mirrors(
                conn,
                make_envelope(
                    "brain",
                    "avatar.state",
                    AvatarStatePayload(emotion="neutral", intensity=0.5, activity="idle"),
                    turn_id=turn_id,
                ),
            )
            # TTS synthesis may run for seconds after the text is complete.
            # turn_end must not wait for it or the shell keeps showing a
            # generating state (and blocks the next question) until audio ends.
            if tts_task is not None:
                await tts_task
        except asyncio.CancelledError:
            logger.info("Turn task cancelled cleanly turn_id=%s", turn_id)
            raise
        except LLMError as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error("Turn failed with LLMError turn_id=%s code=%s", turn_id, exc.code)
            try:
                await _drain_text_delta_buffer()
            except Exception:
                logger.debug(
                    "Failed flushing pending text deltas", exc_info=True
                )
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
            self._turn_end_sent.add(turn_id)
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
            try:
                await _drain_text_delta_buffer()
            except Exception:
                logger.debug(
                    "Failed flushing pending text deltas", exc_info=True
                )
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
            self._turn_end_sent.add(turn_id)
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
            task = text_flush_task
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
            elif task is not None:
                # Consume any exception from an already-finished flush task so
                # asyncio does not emit "Task exception was never retrieved".
                try:
                    task.exception()
                except BaseException:
                    pass
            text_flush_task = None
            text_send_buffer.clear()
            self._turn_cancel_events.pop(turn_id, None)
            self._turn_end_sent.discard(turn_id)
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
        collected_cards: Optional[List[str]] = None,
        collected_card_payloads: Optional[List[Dict[str, Any]]] = None,
        on_text_delta: Optional[Any] = None,
        flush_pending_text: Optional[Any] = None,
    ) -> int:
        for ev in events:
            if isinstance(ev, EmotionEvent):
                if flush_pending_text is not None:
                    await flush_pending_text()
                await self._send_with_mirrors(
                    conn,
                    make_envelope(
                        "brain",
                        "avatar.state",
                        AvatarStatePayload(
                            emotion=ev.emotion,
                            intensity=0.8,
                            activity="speaking",
                        ),
                        turn_id=turn_id,
                    ),
                )
            elif isinstance(ev, TextEvent):
                collected_spoken.append(ev.delta)
                if on_spoken_delta is not None:
                    on_spoken_delta(ev.delta)
                if on_text_delta is not None:
                    await on_text_delta(ev.delta)
                else:
                    seq += 1
                    await self._send_with_mirrors(
                        conn,
                        make_envelope(
                            "brain",
                            "output.text_delta",
                            OutputTextDeltaPayload(seq=seq, delta=ev.delta, is_final=False),
                            turn_id=turn_id,
                        ),
                    )
            elif isinstance(ev, CardEvent):
                if flush_pending_text is not None:
                    await flush_pending_text()
                if collected_cards is not None and ev.content_markdown:
                    collected_cards.append(str(ev.content_markdown))
                actions = []
                if ev.card_type == "spoiler_steps":
                    actions.append(CardAction(id="more_hint", label="还是没头绪，看第 2 级提示"))
                actions.append(CardAction(id="dismiss", label="知道了，收起"))
                card_payload = OutputCardPayload(
                    card_id=ev.card_id,
                    card_type=ev.card_type,
                    title=ev.title,
                    content_markdown=ev.content_markdown,
                    actions=actions,
                )
                if collected_card_payloads is not None:
                    collected_card_payloads.append(card_payload.model_dump())
                self._register_card_actions(ev.card_id, [action.id for action in actions])
                await conn.send_envelope(
                    make_envelope(
                        "brain",
                        "output.card",
                        card_payload,
                        turn_id=turn_id,
                    )
                )
        return seq
