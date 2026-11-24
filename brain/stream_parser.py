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
_ATTR_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')
_MAX_TAG_BUFFER_LEN = 512


def _new_card_id() -> str:
    return f"card_{uuid.uuid4().hex[:10]}"


def _is_possible_prefix(buf: str, target: str) -> bool:
    return target.startswith(buf)


def _match_emotion_tag(buf: str) -> Tuple[str, Union[Emotion, None]]:
    """Return ('match', emotion), ('prefix', None), or ('no', None)."""
    if len(buf) < len(_EMOTION_PREFIX):
        if _is_possible_prefix(buf, _EMOTION_PREFIX):
            return ("prefix", None)
        return ("no", None)
    if not buf.startswith(_EMOTION_PREFIX):
        return ("no", None)

    close_idx = buf.find("]", len(_EMOTION_PREFIX))
    if close_idx == -1:
        if "\n" in buf or len(buf) > 48:
            return ("no", None)
        return ("prefix", None)

    raw_emotion = buf[len(_EMOTION_PREFIX) : close_idx].strip().lower()
    emotion: Emotion = raw_emotion if raw_emotion in ALLOWED_EMOTIONS else "neutral"  # type: ignore[assignment]
    return ("match", emotion)


def _parse_card_attrs(attr_str: str) -> Tuple[str, str]:
    title = "辅助卡片"
    card_type = "markdown"
    for match in _ATTR_RE.finditer(attr_str):
        key = match.group(1).lower()
        val = match.group(2) if match.group(2) is not None else (match.group(3) or "")
        if key == "title" and val:
            title = val
        elif key == "type" and val:
            card_type = val
    return title, card_type


def _match_card_open_tag(buf: str) -> Tuple[str, Tuple[str, str]]:
    """Return ('match', (title, type)), ('prefix', ('', '')), or ('no', ('', ''))."""
    if len(buf) < len(_CARD_OPEN_PREFIX):
        if _is_possible_prefix(buf, _CARD_OPEN_PREFIX):
            return ("prefix", ("", ""))
        return ("no", ("", ""))
    if not buf.startswith(_CARD_OPEN_PREFIX):
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
            elif text:
                self._strip_leading_newline = False
        if text:
            self._text_buffer += text

    def _flush_text(self, events: List[ParserEvent]) -> None:
        if self._text_buffer:
            events.append(TextEvent(delta=self._text_buffer))
            self._text_buffer = ""

    def feed(self, chunk: str) -> List[ParserEvent]:
        """Consume a streaming text chunk and emit zero or more structured events."""
        events: List[ParserEvent] = []
        idx = 0
        n = len(chunk)

        while idx < n:
            if self.state == ParserState.TEXT:
                ch = chunk[idx]
                if ch in ("[", "<"):
                    self._flush_text(events)
                    self.state = ParserState.TAG_BUFFER
                    self._tag_buffer = ch
                    idx += 1
                    continue
                self._emit_text(events, ch)
                idx += 1

            elif self.state == ParserState.TAG_BUFFER:
                self._tag_buffer += chunk[idx]
                idx += 1

                if self._tag_buffer.startswith("["):
                    status, emotion = _match_emotion_tag(self._tag_buffer)
                    if status == "match" and emotion is not None:
                        events.append(EmotionEvent(emotion=emotion))
                        self._tag_buffer = ""
                        self.state = ParserState.TEXT
                        self._strip_leading_newline = True
                    elif status == "no":
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
                        self._flush_text(events)
                        self._card_title, self._card_type = attrs
                        self._card_buffer = ""
                        self._tag_buffer = ""
                        self.state = ParserState.IN_CARD
                    elif status == "no":
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
        """Append slice_str char-by-char until <<<END_CARD>>> is matched or slice exhausted."""
        for i, ch in enumerate(slice_str):
            self._card_buffer += ch
            if self._card_buffer.endswith(_CARD_END_TAG):
                content = self._card_buffer[: -len(_CARD_END_TAG)]
                return (i + 1, content)
        return None

    def finish(self) -> List[ParserEvent]:
        """Finalize stream at EOF, flushing buffers and auto-closing unclosed cards."""
        events: List[ParserEvent] = []
        if self.state == ParserState.TAG_BUFFER:
            if self._tag_buffer:
                self._emit_text(events, self._tag_buffer)
                self._tag_buffer = ""
            self.state = ParserState.TEXT
        elif self.state == ParserState.IN_CARD:
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

        self._flush_text(events)
        return events
