from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import List, Tuple, Union

from protocol.models import ALLOWED_EMOTIONS, Emotion


class ParserState(str, Enum):
    TEXT = "TEXT"
    TAG_BUFFER = "TAG_BUFFER"
    IN_CARD = "IN_CARD"


@dataclass
class TextEvent:
    delta: str


@dataclass
class EmotionEvent:
    emotion: Emotion


@dataclass
class CardEvent:
    card_id: str
    title: str
    card_type: str
    content_markdown: str


ParserEvent = Union[TextEvent, EmotionEvent, CardEvent]

_EMOTION_PREFIX = "[EMOTION:"
_CARD_OPEN_PREFIX = "<<<CARD"
_CARD_END_TAG = "<<<END_CARD>>>"
_ATTR_RE = re.compile(
    r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|“([^”]*)”|‘([^’]*)’)'
)
_FENCE_LINE_RE = re.compile(r"^[ \t]*```[A-Za-z0-9_-]*[ \t]*$")
_MAX_TAG_BUFFER_LEN = 512


def _new_card_id() -> str:
    return f"card_{uuid.uuid4().hex[:10]}"


def _is_possible_prefix(buf: str, target: str) -> bool:
    return target.startswith(buf)


def _match_emotion_tag(buf: str) -> Tuple[str, Union[Emotion, None]]:
    """Return ('match', emotion), ('prefix', None), or ('no', None)."""
    upper = buf.upper()
    if not upper.startswith("["):
        return ("no", None)

    colon_idx = buf.find(":", 1)
    if colon_idx == -1:
        # Still typing '[EMOTION' or '[EMOTION   ' before ':'
        tag_name = upper[1:].rstrip()
        if "\n" in buf or len(buf) > 16:
            return ("no", None)
        if not tag_name or "EMOTION".startswith(tag_name):
            return ("prefix", None)
        return ("no", None)

    tag_name = upper[1:colon_idx].strip()
    if tag_name != "EMOTION":
        return ("no", None)

    close_idx = buf.find("]", colon_idx + 1)
    if close_idx == -1:
        if "\n" in buf or len(buf) > 48:
            return ("no", None)
        return ("prefix", None)

    raw_emotion = buf[colon_idx + 1 : close_idx].strip().lower()
    emotion: Emotion = raw_emotion if raw_emotion in ALLOWED_EMOTIONS else "neutral"  # type: ignore[assignment]
    return ("match", emotion)


def _parse_card_attrs(attr_str: str) -> Tuple[str, str]:
    title = "辅助卡片"
    card_type = "markdown"
    for match in _ATTR_RE.finditer(attr_str):
        key = match.group(1).lower()
        val = next((g for g in match.groups()[1:] if g is not None), "")
        if key == "title" and val:
            title = val
        elif key == "type" and val:
            card_type = val
    return title, card_type


def _match_card_open_tag(buf: str) -> Tuple[str, Tuple[str, str]]:
    """Return ('match', (title, type)), ('prefix', ('', '')), or ('no', ('', ''))."""
    upper = buf.upper()
    if len(upper) < len(_CARD_OPEN_PREFIX):
        if _is_possible_prefix(upper, _CARD_OPEN_PREFIX):
            return ("prefix", ("", ""))
        return ("no", ("", ""))
    if not upper.startswith(_CARD_OPEN_PREFIX):
        return ("no", ("", ""))

    # Require whitespace or '>>>' right after '<<<CARD'
    if len(buf) > len(_CARD_OPEN_PREFIX):
        next_ch = buf[len(_CARD_OPEN_PREFIX)]
        if not next_ch.isspace() and not buf[len(_CARD_OPEN_PREFIX) :].startswith(">>>"):
            if not ">>>".startswith(buf[len(_CARD_OPEN_PREFIX) :]):
                return ("no", ("", ""))

    close_idx = buf.find(">>>", len(_CARD_OPEN_PREFIX))
    if close_idx == -1:
        if "\n" in buf or len(buf) > _MAX_TAG_BUFFER_LEN:
            return ("no", ("", ""))
        return ("prefix", ("", ""))

    attr_str = buf[len(_CARD_OPEN_PREFIX) : close_idx]
    return ("match", _parse_card_attrs(attr_str))


class StreamParser:
    """Mode 1 three-state streaming tag parser (TEXT / TAG_BUFFER / IN_CARD)."""

    def __init__(self) -> None:
        self.state: ParserState = ParserState.TEXT
        self._tag_buffer: str = ""
        self._card_buffer: str = ""
        self._card_title: str = "辅助卡片"
        self._card_type: str = "markdown"
        self._text_buffer: str = ""
        self._strip_leading_newline: bool = False
        self._pending_fence: str = ""
        self._drop_closing_fence: bool = False

    def _emit_text(self, events: List[ParserEvent], text: str) -> None:
        if not text:
            return
        if self._strip_leading_newline:
            if text.startswith("\r\n"):
                text = text[2:]
                self._strip_leading_newline = False
            elif text.startswith("\n"):
                text = text[1:]
                self._strip_leading_newline = False
            elif text[0] in (" ", "\t"):
                # Trim leading single space/tab right after stripped [EMOTION:...] or <<<END_CARD>>>
                text = text.lstrip(" \t")
                if not text:
                    return
                self._strip_leading_newline = False
            elif text:
                self._strip_leading_newline = False
        if not text:
            return

        # Buffer possible fence line (` ``` ` or ` ```markdown `) right before a card block
        # so an LLM wrapping <<<CARD>>> inside a fenced block won't leak stray backticks.
        if self._drop_closing_fence:
            self._pending_fence += text
            if "\n" in self._pending_fence:
                first_line, rest = self._pending_fence.split("\n", 1)
                self._pending_fence = ""
                self._drop_closing_fence = False
                if first_line.strip() == "```":
                    if rest:
                        self._emit_text(events, rest)
                    return
                self._text_buffer += first_line + "\n"
                if rest:
                    self._emit_text(events, rest)
                return
            stripped = self._pending_fence.strip()
            if len(self._pending_fence) <= 8 and (
                not stripped or "```".startswith(stripped) or stripped == "```"
            ):
                return
            buffered = self._pending_fence
            self._pending_fence = ""
            self._drop_closing_fence = False
            self._text_buffer += buffered
            return

        if self._pending_fence:
            self._pending_fence += text
            if "\n" in self._pending_fence:
                first_line, rest = self._pending_fence.split("\n", 1)
                if _FENCE_LINE_RE.match(first_line.rstrip("\r\n")):
                    if rest == "":
                        # Ended right after opening fence line; wait for next char to see if '<' follows
                        return
                    self._text_buffer += first_line + "\n"
                    self._pending_fence = ""
                    self._emit_text(events, rest)
                    return
                self._text_buffer += first_line + "\n"
                self._pending_fence = ""
                if rest:
                    self._emit_text(events, rest)
                return
            stripped = self._pending_fence.strip()
            if len(self._pending_fence) <= 24 and (
                stripped.startswith("```") or "```".startswith(stripped)
            ):
                return
            self._text_buffer += self._pending_fence
            self._pending_fence = ""
            return

        self._text_buffer += text

    def _flush_text(self, events: List[ParserEvent]) -> None:
        if self._text_buffer:
            if self.state == ParserState.TEXT and not self._drop_closing_fence:
                last_line = self._text_buffer.splitlines(keepends=True)[-1]
                stripped = last_line.rstrip("\r\n")
                if _FENCE_LINE_RE.match(stripped) or (
                    not last_line.endswith("\n")
                    and len(stripped) < 24
                    and stripped.startswith("`")
                    and ("```".startswith(stripped) or stripped.startswith("```"))
                ):
                    self._maybe_hold_trailing_fence(allow_partial=True)
            if self._text_buffer:
                events.append(TextEvent(delta=self._text_buffer))
                self._text_buffer = ""

    def _maybe_hold_trailing_fence(self, *, allow_partial: bool = False) -> None:
        """Hold a trailing opening ``` line (or partial ``` prefix at chunk end) in _pending_fence."""
        if self._drop_closing_fence:
            return
        if self._text_buffer:
            lines = self._text_buffer.splitlines(keepends=True)
            last = lines[-1]
            stripped = last.rstrip("\r\n")
            is_full_fence = bool(_FENCE_LINE_RE.match(stripped))
            is_partial_fence = allow_partial and not last.endswith("\n") and (
                stripped.startswith("`") and ("```".startswith(stripped) or stripped.startswith("```"))
            )
            if is_full_fence or is_partial_fence:
                self._pending_fence += last
                self._text_buffer = "".join(lines[:-1])
        # If _pending_fence holds an incomplete line without '\n', wait until '\n' before '<'
        # Otherwise if _pending_fence IS a full fence line ending with '\n', keep it held for '<'!

    def _flush_pending_fence_to_text(self) -> None:
        if self._pending_fence and not self._drop_closing_fence:
            self._text_buffer += self._pending_fence
            self._pending_fence = ""

    def feed(self, chunk: str) -> List[ParserEvent]:
        """Consume a streaming text chunk and emit zero or more structured events."""
        events: List[ParserEvent] = []
        idx = 0
        n = len(chunk)

        while idx < n:
            if self.state == ParserState.TEXT:
                ch = chunk[idx]
                if ch in ("[", "<"):
                    if ch == "<":
                        self._maybe_hold_trailing_fence()
                    else:
                        self._flush_pending_fence_to_text()
                    self._flush_text(events)
                    self.state = ParserState.TAG_BUFFER
                    self._tag_buffer = ch
                    idx += 1
                    continue
                if self._pending_fence and not self._drop_closing_fence:
                    # Only flush if _pending_fence already has a newline and current char isn't '<'
                    if "\n" in self._pending_fence:
                        self._flush_pending_fence_to_text()
                self._emit_text(events, ch)
                idx += 1

            elif self.state == ParserState.TAG_BUFFER:
                self._tag_buffer += chunk[idx]
                idx += 1

                if self._tag_buffer.startswith("["):
                    status, emotion = _match_emotion_tag(self._tag_buffer)
                    if status == "match" and emotion is not None:
                        self._flush_pending_fence_to_text()
                        events.append(EmotionEvent(emotion=emotion))
                        self._tag_buffer = ""
                        self.state = ParserState.TEXT
                        self._strip_leading_newline = True
                    elif status == "no":
                        self._flush_pending_fence_to_text()
                        bad = self._tag_buffer
                        self._tag_buffer = ""
                        self.state = ParserState.TEXT
                        self._emit_text(events, bad[0])
                        if len(bad) > 1:
                            chunk = bad[1:] + chunk[idx:]
                            idx = 0
                            n = len(chunk)
                else:
                    status, attrs = _match_card_open_tag(self._tag_buffer)
                    if status == "match":
                        if self._pending_fence:
                            self._pending_fence = ""
                            self._drop_closing_fence = True
                        self._flush_text(events)
                        self._card_title, self._card_type = attrs
                        self._card_buffer = ""
                        self._tag_buffer = ""
                        self.state = ParserState.IN_CARD
                    elif status == "no":
                        self._flush_pending_fence_to_text()
                        bad = self._tag_buffer
                        self._tag_buffer = ""
                        self.state = ParserState.TEXT
                        self._emit_text(events, bad[0])
                        if len(bad) > 1:
                            chunk = bad[1:] + chunk[idx:]
                            idx = 0
                            n = len(chunk)

            elif self.state == ParserState.IN_CARD:
                # Consume remaining chunk up to potential end tag quickly
                end_pos = self._card_buffer_append_and_check(chunk[idx:])
                if end_pos is not None:
                    consumed_len, card_content = end_pos
                    idx += consumed_len
                    events.append(
                        CardEvent(
                            card_id=_new_card_id(),
                            title=self._card_title,
                            card_type=self._card_type,
                            content_markdown=card_content.strip(),
                        )
                    )
                    self._card_buffer = ""
                    self.state = ParserState.TEXT
                    self._strip_leading_newline = True
                else:
                    idx = n

        self._flush_text(events)
        return events

    def _card_buffer_append_and_check(self, slice_str: str) -> Union[Tuple[int, str], None]:
        """Append slice_str char-by-char until <<<END_CARD>>> is matched (case-insensitive) or exhausted."""
        for i, ch in enumerate(slice_str):
            self._card_buffer += ch
            if len(self._card_buffer) >= len(_CARD_END_TAG) and self._card_buffer[-len(_CARD_END_TAG) :].upper() == _CARD_END_TAG:
                content = self._card_buffer[: -len(_CARD_END_TAG)]
                return (i + 1, content)
        return None

    def finish(self) -> List[ParserEvent]:
        """Finalize stream at EOF, flushing buffers and auto-closing unclosed cards."""
        events: List[ParserEvent] = []
        if self.state == ParserState.TAG_BUFFER:
            self._flush_pending_fence_to_text()
            if self._tag_buffer:
                self._emit_text(events, self._tag_buffer)
                self._tag_buffer = ""
            self.state = ParserState.TEXT
        elif self.state == ParserState.IN_CARD:
            self._drop_closing_fence = False
            self._pending_fence = ""
            # Auto-close unclosed card on EOF so content is never lost
            events.append(
                CardEvent(
                    card_id=_new_card_id(),
                    title=self._card_title,
                    card_type=self._card_type,
                    content_markdown=self._card_buffer.strip(),
                )
            )
            self._card_buffer = ""
            self.state = ParserState.TEXT

        if self._pending_fence and not self._drop_closing_fence:
            self._text_buffer += self._pending_fence
            self._pending_fence = ""

        if self._text_buffer:
            events.append(TextEvent(delta=self._text_buffer))
            self._text_buffer = ""
        return events
