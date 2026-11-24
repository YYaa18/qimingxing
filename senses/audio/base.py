from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Dict, Tuple

logger = logging.getLogger("qimingxing.senses.audio")

# macOS Quartz virtual keycodes & modifier flag masks
_MOD_FLAG_ALT = 0x00080000
_MOD_FLAG_CTRL = 0x00040000
_MOD_FLAG_CMD = 0x00100000
_MOD_FLAG_SHIFT = 0x00020000
_MOD_FLAG_FN = 0x00800000

_MODIFIER_ALIASES: Dict[str, Tuple[Tuple[int, ...], int]] = {
    "right_option": ((61,), _MOD_FLAG_ALT),
    "right_alt": ((61,), _MOD_FLAG_ALT),
    "left_option": ((58,), _MOD_FLAG_ALT),
    "left_alt": ((58,), _MOD_FLAG_ALT),
    "option": ((58, 61), _MOD_FLAG_ALT),
    "alt": ((58, 61), _MOD_FLAG_ALT),
    "right_ctrl": ((62,), _MOD_FLAG_CTRL),
    "left_ctrl": ((59,), _MOD_FLAG_CTRL),
    "ctrl": ((59, 62), _MOD_FLAG_CTRL),
    "control": ((59, 62), _MOD_FLAG_CTRL),
    "right_cmd": ((54,), _MOD_FLAG_CMD),
    "left_cmd": ((55,), _MOD_FLAG_CMD),
    "cmd": ((55, 54), _MOD_FLAG_CMD),
    "command": ((55, 54), _MOD_FLAG_CMD),
    "shift": ((56, 60), _MOD_FLAG_SHIFT),
    "fn": ((63,), _MOD_FLAG_FN),
}

_KEY_NAME_TO_CODE: Dict[str, int] = {
    "space": 49,
    "return": 36,
    "enter": 36,
    "tab": 48,
    "esc": 53,
    "escape": 53,
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5, "h": 4,
    "i": 34, "j": 38, "k": 40, "l": 37, "m": 46, "n": 45, "o": 31, "p": 35,
    "q": 12, "r": 15, "s": 1, "t": 17, "u": 32, "v": 9, "w": 13, "x": 7,
    "y": 16, "z": 6,
    "0": 29, "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26,
    "8": 28, "9": 25,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}


@dataclass(frozen=True)
class HotkeySpec:
    raw_spec: str
    is_modifier_only: bool
    key_codes: Tuple[int, ...]
    modifier_mask: int
    is_fallback: bool = False


def is_disabled_hotkey(spec: str) -> bool:
    """Return True when a configured shortcut is intentionally disabled."""
    return str(spec or "").strip().lower() in ("", "disabled", "off", "none")


def parse_hotkey_spec(spec: str) -> HotkeySpec:
    """Parse configurable PTT hotkey specs like 'right_option', 'option+space', 'ctrl+space'."""
    normalized = "+".join(part.strip().lower() for part in str(spec or "").split("+") if part.strip())
    if not normalized:
        return HotkeySpec(
            raw_spec="right_option",
            is_modifier_only=True,
            key_codes=(61,),
            modifier_mask=_MOD_FLAG_ALT,
            is_fallback=True,
        )

    if normalized in _MODIFIER_ALIASES:
        codes, mask = _MODIFIER_ALIASES[normalized]
        return HotkeySpec(
            raw_spec=normalized,
            is_modifier_only=True,
            key_codes=codes,
            modifier_mask=mask,
        )

    parts = [p for p in normalized.split("+") if p]
    if len(parts) == 1 and parts[0] in _KEY_NAME_TO_CODE:
        return HotkeySpec(
            raw_spec=normalized,
            is_modifier_only=False,
            key_codes=(_KEY_NAME_TO_CODE[parts[0]],),
            modifier_mask=0,
        )

    if len(parts) >= 2:
        key_part = parts[-1]
        mod_parts = parts[:-1]
        if key_part in _KEY_NAME_TO_CODE and all(m in _MODIFIER_ALIASES for m in mod_parts):
            mask = 0
            for m in mod_parts:
                mask |= _MODIFIER_ALIASES[m][1]
            return HotkeySpec(
                raw_spec=normalized,
                is_modifier_only=False,
                key_codes=(_KEY_NAME_TO_CODE[key_part],),
                modifier_mask=mask,
            )

    logger.warning("Unsupported audio.ptt.hotkey '%s'; falling back to 'right_option'", spec)
    return HotkeySpec(
        raw_spec="right_option",
        is_modifier_only=True,
        key_codes=(61,),
        modifier_mask=_MOD_FLAG_ALT,
        is_fallback=True,
    )


@dataclass
class AudioPermissionStatus:
    microphone: bool = True
    accessibility: bool = True
    hint: str = ""


class HotkeyListener(ABC):
    """Abstract global Push-to-Talk hotkey listener."""

    @abstractmethod
    def start(
        self,
        *,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
    ) -> None:
        """Start listening for press/release transitions."""

    @abstractmethod
    def stop(self) -> None:
        """Stop background listener cleanly."""


class AudioCapturer(ABC):
    """Abstract 16kHz mono signed 16-bit PCM microphone capturer."""

    @abstractmethod
    def start(self, on_pcm_chunk: Callable[[bytes], None]) -> None:
        """Start capturing PCM chunks and invoking `on_pcm_chunk`."""

    @abstractmethod
    def stop(self) -> None:
        """Stop recording and release audio device."""
