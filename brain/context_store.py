from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional

from protocol.models import (
    ContextGameStatePayload,
    ContextOcrSnippetPayload,
    ContextWindowChangedPayload,
    now_ms,
)

MAX_TIMELINE_ENTRIES = 20
TIMELINE_WINDOW_SECONDS = 1800  # 30 minutes
MIN_TRANSIENT_SECONDS = 3.0


@dataclass
class ActivityEntry:
    app_name: str
    window_title: str
    scene: str
    started_at: float
    ended_at: float
    privacy_masked: bool = False
    is_self_window: bool = False

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.ended_at - self.started_at)

    def format_line(self) -> str:
        start_str = datetime.fromtimestamp(self.started_at).strftime("%H:%M")
        end_str = datetime.fromtimestamp(self.ended_at).strftime("%H:%M")
        title_part = f" | {self.window_title}" if self.window_title else ""
        return f"{start_str}–{end_str} | {self.app_name}{title_part}"


@dataclass
class ContextSnapshot:
    window_changed: Optional[ContextWindowChangedPayload] = None
    window_updated_at: Optional[int] = None
    last_non_self_window: Optional[ContextWindowChangedPayload] = None
    game_state: Optional[ContextGameStatePayload] = None
    game_updated_at: Optional[int] = None
    ocr_snippet: Optional[ContextOcrSnippetPayload] = None
    ocr_updated_at: Optional[int] = None
    timeline: List[ActivityEntry] = field(default_factory=list)


class ContextStore:
    """In-memory store of active desktop window, 30-min merged activity timeline, and senses context."""

    def __init__(
        self,
        *,
        clock: Optional[Callable[[], float]] = None,
        max_timeline_entries: int = MAX_TIMELINE_ENTRIES,
        timeline_window_seconds: float = TIMELINE_WINDOW_SECONDS,
        min_transient_seconds: float = MIN_TRANSIENT_SECONDS,
    ) -> None:
        self._clock = clock or time.time
        self._max_timeline_entries = max_timeline_entries
        self._timeline_window_seconds = timeline_window_seconds
        self._min_transient_seconds = min_transient_seconds
        self._snapshot = ContextSnapshot()

    def update_window(self, payload: ContextWindowChangedPayload) -> None:
        now = self._clock()
        self._snapshot.window_changed = payload
        self._snapshot.window_updated_at = now_ms()
        if not payload.is_self_window and not payload.privacy_masked:
            self._snapshot.last_non_self_window = payload

        self._record_timeline(payload, now)

    def _record_timeline(self, payload: ContextWindowChangedPayload, now: float) -> None:
        timeline = self._snapshot.timeline
        title = payload.window_title if not payload.privacy_masked else "[隐私窗口已隐藏]"
        app_name = payload.app_name if not payload.privacy_masked else "[隐私窗口已隐藏]"

        if timeline:
            last = timeline[-1]
            # Merge consecutive identical windows
            if (
                last.app_name == app_name
                and last.window_title == title
                and last.privacy_masked == payload.privacy_masked
            ):
                last.ended_at = now
                self._prune_timeline(now)
                return

            # Extend previous entry up to transition timestamp
            last.ended_at = now
            # Drop ultra-short transient window noise (< min_transient_seconds) unless it was self/privacy
            if (
                not payload.is_self_window
                and last.duration_seconds < self._min_transient_seconds
                and len(timeline) >= 1
                and not last.privacy_masked
            ):
                timeline.pop()
                # If popping exposed an identical previous entry, merge into it
                if (
                    timeline
                    and timeline[-1].app_name == app_name
                    and timeline[-1].window_title == title
                    and timeline[-1].privacy_masked == payload.privacy_masked
                ):
                    timeline[-1].ended_at = now
                    self._prune_timeline(now)
                    return

        if payload.is_self_window:
            self._prune_timeline(now)
            return

        timeline.append(
            ActivityEntry(
                app_name=app_name,
                window_title=title,
                scene=payload.scene,
                started_at=now,
                ended_at=now,
                privacy_masked=payload.privacy_masked,
                is_self_window=payload.is_self_window,
            )
        )
        self._prune_timeline(now)

    def _prune_timeline(self, now: float) -> None:
        cutoff = now - self._timeline_window_seconds
        self._snapshot.timeline = [
            entry for entry in self._snapshot.timeline if entry.ended_at >= cutoff
        ][-self._max_timeline_entries :]

    def update_game_state(self, payload: ContextGameStatePayload) -> None:
        self._snapshot.game_state = payload
        self._snapshot.game_updated_at = now_ms()

    def update_ocr(self, payload: ContextOcrSnippetPayload) -> None:
        self._snapshot.ocr_snippet = payload
        self._snapshot.ocr_updated_at = now_ms()

    def clear_window_context(self) -> None:
        """Clear active window & timeline (e.g. when switching privacy mode to OFF)."""
        self._snapshot.window_changed = None
        self._snapshot.last_non_self_window = None
        self._snapshot.window_updated_at = None
        self._snapshot.timeline.clear()

    def get_snapshot(self) -> ContextSnapshot:
        return self._snapshot

    def get_timeline(self) -> List[ActivityEntry]:
        self._prune_timeline(self._clock())
        return list(self._snapshot.timeline)

    def get_window_summary(self) -> str:
        w = self._snapshot.last_non_self_window or self._snapshot.window_changed
        if w is None:
            return ""
        if w.privacy_masked:
            return "[隐私窗口已隐藏]"
        if w.window_title:
            return f"{w.app_name} — {w.window_title}"
        return w.app_name

    def get_sanitized_timeline_for_footprint(self) -> List[ActivityEntry]:
        """Return recent non-self, non-masked activity entries safe for L3 persistence."""
        return [
            entry
            for entry in self.get_timeline()
            if not entry.privacy_masked and not entry.is_self_window
        ]

    def render_compact_focus(self) -> str:
        """Render a single concise focus line for ordinary chats (minimal noise)."""
        parts: list[str] = []
        w = self._snapshot.last_non_self_window or self._snapshot.window_changed
        if w is not None:
            if w.privacy_masked:
                parts.append("当前焦点: [隐私窗口已隐藏]")
            else:
                title = f" ({w.window_title})" if w.window_title else ""
                parts.append(f"当前焦点: {w.app_name}{title} [scene={w.scene}]")
        if self._snapshot.game_state is not None:
            g = self._snapshot.game_state
            data_json = json.dumps(g.data, ensure_ascii=False)
            parts.append(
                f"当前游戏状态 (game_id={g.game_id}, provider={g.provider}, "
                f"version={g.summary_version}): {data_json}"
            )
        if self._snapshot.ocr_snippet is not None:
            o = self._snapshot.ocr_snippet
            parts.append(f"最近 OCR 片段 (region={o.region}): {o.text}")
        if not parts:
            return "暂无外部感官上下文。"
        return "\n".join(parts)

    def render_full_timeline(self) -> str:
        return self.render_for_prompt()

    def render_for_prompt(self) -> str:
        """Render active desktop context, 30-min timeline, and optional senses metadata."""
        parts: list[str] = []
        if self._snapshot.window_changed is not None:
            w = self._snapshot.window_changed
            if w.privacy_masked:
                parts.append("- 当前活跃窗口: [隐私窗口已隐藏]")
            else:
                self_tag = " (启明星自身窗口)" if w.is_self_window else ""
                parts.append(
                    f"- 当前活跃窗口{self_tag}: app={w.app_name}, scene={w.scene}, "
                    f"title={w.window_title or 'N/A'}"
                )

        if (
            self._snapshot.last_non_self_window is not None
            and self._snapshot.last_non_self_window is not self._snapshot.window_changed
        ):
            nw = self._snapshot.last_non_self_window
            parts.append(
                f"- 最近目标工作窗口: app={nw.app_name}, scene={nw.scene}, "
                f"title={nw.window_title or 'N/A'}"
            )

        timeline = self.get_timeline()
        if timeline:
            lines = "\n".join(f"  * {entry.format_line()}" for entry in timeline[-10:])
            parts.append(f"- 最近桌面活动时间线（最近 30 分钟）:\n{lines}")

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
