from __future__ import annotations

import re
from typing import List

_SENTENCE_END_CHARS = set("。！？!?；;\n")
_SOFT_BREAK_CHARS = set("，,、 ")
_CONTROL_TAG_RE = re.compile(r"\[(?:EMOTION|CARD)\s*[:=][^\]]*\]", re.IGNORECASE)
_CODE_FENCE_MARKER = "```"


def _clean_for_speech(text: str) -> str:
    cleaned = _CONTROL_TAG_RE.sub("", text)
    # Strip markdown emphasis/inline-code wrappers while keeping readable words
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
    # Collapse excess spaces
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned.strip()


class SentenceChunker:
    """Incrementally splits streamed LLM text deltas into speakable clauses while silencing code & cards."""

    def __init__(self, *, min_chars: int = 4, max_chars: int = 120) -> None:
        self.min_chars = max(1, int(min_chars))
        self.max_chars = max(self.min_chars + 10, int(max_chars))
        self._raw_buf: str = ""
        self._spoken_buf: str = ""
        self._in_code_fence: bool = False
        self._in_card_block: bool = False

    def feed(self, delta: str) -> List[str]:
        if not delta:
            return []
        self._raw_buf += delta
        self._drain_raw(final=False)
        return self._pop_ready_sentences(final=False)

    def flush(self) -> List[str]:
        self._drain_raw(final=True)
        return self._pop_ready_sentences(final=True)

    def _drain_raw(self, *, final: bool) -> None:
        while self._raw_buf:
            if self._in_code_fence:
                end_idx = self._raw_buf.find(_CODE_FENCE_MARKER)
                if end_idx == -1:
                    if final:
                        self._raw_buf = ""
                    elif len(self._raw_buf) > 4:
                        self._raw_buf = self._raw_buf[-4:]
                    return
                self._raw_buf = self._raw_buf[end_idx + len(_CODE_FENCE_MARKER) :]
                self._in_code_fence = False
                continue

            if self._in_card_block:
                end_idx = self._raw_buf.upper().find("[/CARD]")
                if end_idx == -1:
                    if final:
                        self._raw_buf = ""
                    elif len(self._raw_buf) > 8:
                        self._raw_buf = self._raw_buf[-8:]
                    return
                self._raw_buf = self._raw_buf[end_idx + 7 :]
                self._in_card_block = False
                continue

            # Look for ``` code fence or [EMOTION: / [CARD: tags
            fence_idx = self._raw_buf.find(_CODE_FENCE_MARKER)
            tag_idx = self._raw_buf.find("[")
            candidates = [i for i in (fence_idx, tag_idx) if i != -1]
            if not candidates:
                # Hold up to 2 trailing backticks in case ``` spans chunks
                if not final and self._raw_buf.endswith(("`", "``")):
                    keep = 2 if self._raw_buf.endswith("``") else 1
                    self._spoken_buf += self._raw_buf[:-keep]
                    self._raw_buf = self._raw_buf[-keep:]
                else:
                    self._spoken_buf += self._raw_buf
                    self._raw_buf = ""
                return

            first_idx = min(candidates)
            if first_idx > 0:
                self._spoken_buf += self._raw_buf[:first_idx]
                self._raw_buf = self._raw_buf[first_idx:]
                continue

            if self._raw_buf.startswith(_CODE_FENCE_MARKER):
                self._in_code_fence = True
                self._raw_buf = self._raw_buf[len(_CODE_FENCE_MARKER) :]
                continue

            # Starts with '[' -> check control tag
            upper_head = self._raw_buf[:12].upper()
            if not final and ("[" in upper_head) and len(self._raw_buf) < 10 and (
                "[EMOTION".startswith(upper_head) or "[CARD".startswith(upper_head)
            ):
                return

            if upper_head.startswith("[CARD:"):
                self._in_card_block = True
                self._raw_buf = self._raw_buf[6:]
                continue

            emotion_head = upper_head[:8] == "[EMOTION"
            if emotion_head and len(self._raw_buf) > 8 and self._raw_buf[8] in ":=":
                close_idx = self._raw_buf.find("]")
                if close_idx == -1:
                    if final:
                        self._raw_buf = ""
                    return
                self._raw_buf = self._raw_buf[close_idx + 1 :]
                continue

            # Regular literal '['
            self._spoken_buf += "["
            self._raw_buf = self._raw_buf[1:]

    def _pop_ready_sentences(self, *, final: bool) -> List[str]:
        out: List[str] = []
        while self._spoken_buf:
            split_idx = -1
            for idx, ch in enumerate(self._spoken_buf):
                if ch in _SENTENCE_END_CHARS and idx + 1 >= self.min_chars:
                    split_idx = idx + 1
                    break
            if split_idx != -1:
                raw_chunk = self._spoken_buf[:split_idx]
                self._spoken_buf = self._spoken_buf[split_idx:]
                cleaned = _clean_for_speech(raw_chunk)
                if cleaned and any(c.isalnum() for c in cleaned):
                    out.append(cleaned)
                continue

            if len(self._spoken_buf) >= self.max_chars:
                soft_idx = -1
                for idx in range(min(self.max_chars, len(self._spoken_buf)) - 1, self.min_chars - 1, -1):
                    if self._spoken_buf[idx] in _SOFT_BREAK_CHARS:
                        soft_idx = idx + 1
                        break
                cut = soft_idx if soft_idx != -1 else self.max_chars
                raw_chunk = self._spoken_buf[:cut]
                self._spoken_buf = self._spoken_buf[cut:]
                cleaned = _clean_for_speech(raw_chunk)
                if cleaned and any(c.isalnum() for c in cleaned):
                    out.append(cleaned)
                continue

            break

        if final and self._spoken_buf:
            cleaned = _clean_for_speech(self._spoken_buf)
            self._spoken_buf = ""
            if cleaned and any(c.isalnum() for c in cleaned):
                out.append(cleaned)
        return out
