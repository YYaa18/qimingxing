from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from fastapi import WebSocket
from pydantic import ValidationError

from brain.context_store import ContextStore
from brain.llm_router import LLMError, LLMRouter
from brain.prompts import build_system_prompt
from brain.stream_parser import CardEvent, EmotionEvent, StreamParser, TextEvent
from protocol.models import (
    AvatarStatePayload,
    CardAction,
    ClientCapabilities,
    ContextGameStatePayload,
    ContextOcrSnippetPayload,
    ContextWindowChangedPayload,
    Envelope,
    InputActionPayload,
    InputCancelPayload,
    InputTextPayload,
    OutputCardPayload,
    OutputTextDeltaPayload,
    OutputTurnEndPayload,
    OutputTurnStartPayload,
    SourceRole,
    SystemConfigSetPayload,
    SystemErrorPayload,
    SystemHelloPayload,
    SystemPingPayload,
    SystemPongPayload,
    SystemWelcomePayload,
    make_envelope,
    new_turn_id,
    parse_envelope,
    parse_payload,
)

logger = logging.getLogger("qimingxing.hub")

SERVER_VERSION = "0.1.0-mvp1"


@dataclass
class ClientConnection:
    client_id: str
    websocket: WebSocket
    role: Optional[SourceRole] = None
    client_name: str = "unknown"
    capabilities: ClientCapabilities = field(default_factory=ClientCapabilities)
    active_turn_id: Optional[str] = None
    active_task: Optional[asyncio.Task[None]] = None
    history: List[Dict[str, str]] = field(default_factory=list)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send_envelope(self, envelope: Envelope) -> None:
        async with self.send_lock:
            await self.websocket.send_text(envelope.model_dump_json())


class Hub:
    """Central star-topology WebSocket hub managing Body and Senses clients."""

    def __init__(
        self,
        llm_router: LLMRouter,
        context_store: Optional[ContextStore] = None,
    ) -> None:
        self.llm_router = llm_router
        self.context_store = context_store or ContextStore()
        self.clients: Dict[str, ClientConnection] = {}
        self._conn_seq = 0

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
            logger.debug(
                "Ignoring unknown event type=%s from client=%s",
                envelope.type,
                conn.client_id,
            )
            return

        await self._dispatch(conn, envelope, payload)

    async def _dispatch(self, conn: ClientConnection, env: Envelope, payload: object) -> None:
        if isinstance(payload, SystemHelloPayload):
            conn.role = payload.role
            conn.client_name = payload.client_name
            conn.capabilities = payload.capabilities
            logger.info(
                "Handshake hello: client=%s role=%s name=%s avatar_mode=%s",
                conn.client_id,
                conn.role,
                conn.client_name,
                conn.capabilities.avatar_mode,
            )
            await self._send_welcome(conn)
            return

        if isinstance(payload, SystemPingPayload):
            await conn.send_envelope(
                make_envelope(
                    "brain",
                    "system.pong",
                    SystemPongPayload(client_ts=payload.client_ts),
                )
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
                        SystemErrorPayload(
                            code=exc.code,
                            message=exc.message,
                            recoverable=exc.recoverable,
                        ),
                    )
                )
                return
            await self._broadcast_welcome_to_bodies()
            return

        if isinstance(payload, ContextWindowChangedPayload):
            self.context_store.update_window(payload)
            logger.info("Updated context.window_changed: app=%s scene=%s", payload.app_name, payload.scene)
            return

        if isinstance(payload, ContextGameStatePayload):
            self.context_store.update_game_state(payload)
            logger.info(
                "Updated context.game_state: game_id=%s provider=%s ver=%d",
                payload.game_id,
                payload.provider,
                payload.summary_version,
            )
            return

        if isinstance(payload, ContextOcrSnippetPayload):
            self.context_store.update_ocr(payload)
            logger.info("Updated context.ocr_snippet: region=%s", payload.region)
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
            await self._start_turn(conn, turn_id, payload.text)
            return

        if isinstance(payload, InputActionPayload):
            turn_id = env.turn_id or new_turn_id()
            prompt_text = self._action_to_prompt(payload)
            await self._start_turn(conn, turn_id, prompt_text)
            return

    def _action_to_prompt(self, payload: InputActionPayload) -> str:
        if payload.action == "more_hint":
            return "请基于刚才的防剧透提示，给出更进一步的第 2 级提示（依然尽量避免直接剧透结局）。"
        return f"用户触发快捷动作 `{payload.action}`（参数: {payload.data}），请简短响应。"

    async def _send_welcome(self, conn: ClientConnection) -> None:
        welcome = SystemWelcomePayload(
            server_version=SERVER_VERSION,
            active_profile=self.llm_router.active_profile,
            profiles=self.llm_router.list_public_profiles(),
        )
        await conn.send_envelope(make_envelope("brain", "system.welcome", welcome))

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
            logger.debug(
                "Cancel ignored for stale turn_id=%s (active=%s)",
                target_turn_id,
                old_turn_id,
            )
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

    async def _start_turn(self, conn: ClientConnection, turn_id: str, user_text: str) -> None:
        await self._cancel_active_turn(conn, notify=True)
        conn.active_turn_id = turn_id
        task = asyncio.create_task(self._run_turn(conn, turn_id, user_text))
        conn.active_task = task

    async def _run_turn(self, conn: ClientConnection, turn_id: str, user_text: str) -> None:
        started = time.perf_counter()
        profile = self.llm_router.get_profile()
        parser = StreamParser()
        seq = 0
        collected_spoken: List[str] = []

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

            system_prompt = build_system_prompt(self.context_store)
            messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
            messages.extend(conn.history[-12:])
            messages.append({"role": "user", "content": user_text})

            async for chunk in self.llm_router.stream_chat(messages, profile_name=profile.name):
                events = parser.feed(chunk)
                seq = await self._emit_parser_events(conn, turn_id, events, seq, collected_spoken)

            tail_events = parser.finish()
            seq = await self._emit_parser_events(conn, turn_id, tail_events, seq, collected_spoken)

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            assistant_reply = "".join(collected_spoken).strip()
            conn.history.append({"role": "user", "content": user_text})
            if assistant_reply:
                conn.history.append({"role": "assistant", "content": assistant_reply})

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
