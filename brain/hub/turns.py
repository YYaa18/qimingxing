from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from protocol.models import (
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
