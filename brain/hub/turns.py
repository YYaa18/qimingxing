from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from protocol.models import (
    ContextScreenRefPayload,
    OutputToolStatusPayload,
    SensesCaptureRequestPayload,
    SystemCaptureDonePayload,
    SystemCapturePreparePayload,
    AvatarStatePayload,
    CardAction,
    Envelope,
    OutputCardPayload,
    OutputTextDeltaPayload,
    make_envelope,
)

from brain.stream_parser import CardEvent, EmotionEvent, TextEvent

if TYPE_CHECKING:  # pragma: no cover - typing only
    from brain.hub.connection import ClientConnection

logger = logging.getLogger("qimingxing.hub.turns")


class TurnStreamMixin:
    """Turn streaming emit helpers (moved verbatim from Hub)."""

    def _register_card_actions(self, card_id: str, action_ids: List[str]) -> None:
        if not card_id:
            return
        self._card_actions[card_id] = set(action_ids)
        while len(self._card_actions) > self._card_action_limit:
            oldest = next(iter(self._card_actions))
            self._card_actions.pop(oldest, None)

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

