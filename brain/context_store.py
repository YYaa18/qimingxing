from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from protocol.models import (
    ContextGameStatePayload,
    ContextOcrSnippetPayload,
    ContextWindowChangedPayload,
    now_ms,
)


@dataclass
class ContextSnapshot:
    window_changed: Optional[ContextWindowChangedPayload] = None
    window_updated_at: Optional[int] = None
    game_state: Optional[ContextGameStatePayload] = None
    game_updated_at: Optional[int] = None
    ocr_snippet: Optional[ContextOcrSnippetPayload] = None
    ocr_updated_at: Optional[int] = None


class ContextStore:
    """In-memory store of the latest senses context pushed via WebSocket."""

    def __init__(self) -> None:
        self._snapshot = ContextSnapshot()

    def update_window(self, payload: ContextWindowChangedPayload) -> None:
        self._snapshot.window_changed = payload
        self._snapshot.window_updated_at = now_ms()

    def update_game_state(self, payload: ContextGameStatePayload) -> None:
        self._snapshot.game_state = payload
        self._snapshot.game_updated_at = now_ms()

    def update_ocr(self, payload: ContextOcrSnippetPayload) -> None:
        self._snapshot.ocr_snippet = payload
        self._snapshot.ocr_updated_at = now_ms()

    def get_snapshot(self) -> ContextSnapshot:
        return self._snapshot

    def render_for_prompt(self) -> str:
        """Render active senses context as concise Markdown for the system prompt."""
        parts: list[str] = []
        if self._snapshot.window_changed is not None:
            w = self._snapshot.window_changed
            parts.append(
                f"- 当前活跃窗口: app={w.app_name}, scene={w.scene}, "
                f"process={w.process_name or 'N/A'}, title={w.window_title or 'N/A'}"
            )
        if self._snapshot.game_state is not None:
            g = self._snapshot.game_state
            data_json = json.dumps(g.data, ensure_ascii=False)
            parts.append(
                f"- 当前游戏状态 (game_id={g.game_id}, provider={g.provider}, "
                f"version={g.summary_version}): {data_json}"
            )
        if self._snapshot.ocr_snippet is not None:
            o = self._snapshot.ocr_snippet
            parts.append(
                f"- 最近 OCR 片段 (region={o.region}, confidence={o.confidence:.2f}): {o.text}"
            )
        if not parts:
            return "暂无外部感官上下文。"
        return "\n".join(parts)
