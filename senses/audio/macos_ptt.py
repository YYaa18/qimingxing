from __future__ import annotations

import ctypes
import logging
import sys
import threading
from typing import Any, Callable, Optional

from senses.audio.base import (
    AudioPermissionStatus,
    HotkeyListener,
    HotkeySpec,
    parse_hotkey_spec,
)

logger = logging.getLogger("qimingxing.senses.macos_ptt")


def _check_ax_trusted() -> bool:
    if sys.platform != "darwin":
        return True
    try:
        app_svc = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        app_svc.AXIsProcessTrusted.restype = ctypes.c_bool
        if bool(app_svc.AXIsProcessTrusted()):
            return True
    except Exception:
        pass
    try:
        from Quartz import CGPreflightListenEventAccess  # type: ignore[import-not-found]

        return bool(CGPreflightListenEventAccess())
    except Exception:
        return False


def _check_microphone_access() -> bool:
    if sys.platform != "darwin":
        return True
    try:
        import objc  # type: ignore[import-not-found]

        bundle = objc.loadBundle(
            "AVFoundation",
            {},
            bundle_path="/System/Library/Frameworks/AVFoundation.framework",
        )
        cls = objc.lookUpClass("AVCaptureDevice")
        # AVAuthorizationStatus: 0=NotDetermined, 1=Restricted, 2=Denied, 3=Authorized
        status = int(cls.authorizationStatusForMediaType_("soun"))
        if status in (1, 2):
            return False
    except Exception:
        pass
    try:
        import sounddevice as sd

        devs = sd.query_devices()
        return any(int(d.get("max_input_channels", 0)) > 0 for d in devs)
    except Exception:
        return False


def check_macos_audio_permissions() -> AudioPermissionStatus:
    """Inspect Accessibility/Input Monitoring and Microphone permissions without crashing."""
    ax_ok = _check_ax_trusted()
    mic_ok = _check_microphone_access()
    hints = []
    if not mic_ok:
        hints.append("请在「系统设置 → 隐私与安全性 → 麦克风」中允许当前终端/Python 使用麦克风。")
    if not ax_ok:
        hints.append(
            "请在「系统设置 → 隐私与安全性 → 辅助功能」与「输入监控」中授权当前终端/Python 以启用全局按住说话热键。"
        )
    return AudioPermissionStatus(
        microphone=mic_ok,
        accessibility=ax_ok,
        hint=" ".join(hints),
    )


class MacOSPTTListener(HotkeyListener):
    """Global macOS Push-to-Talk hotkey listener backed by Quartz CGEventTap."""

    def __init__(
        self,
        hotkey_spec: str = "right_option",
        *,
        permission_checker: Optional[Callable[[], AudioPermissionStatus]] = None,
    ) -> None:
        self.spec: HotkeySpec = parse_hotkey_spec(hotkey_spec)
        self._permission_checker = permission_checker or check_macos_audio_permissions
        self._thread: Optional[threading.Thread] = None
        self._run_loop: Any = None
        self._tap: Any = None
        self._stop_event = threading.Event()
        self._pressed = False
        self._on_press: Optional[Callable[[], None]] = None
        self._on_release: Optional[Callable[[], None]] = None

    def start(
        self,
        *,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
    ) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._on_press = on_press
        self._on_release = on_release
        self._stop_event.clear()

        perm = self._permission_checker()
        if not perm.accessibility or not perm.microphone:
            logger.warning("macOS Audio/PTT 权限未就绪（仍将继续启动其余功能）: %s", perm.hint)
            if not perm.accessibility:
                return

        if sys.platform != "darwin":
            logger.info("MacOSPTTListener disabled on non-macOS platform '%s'", sys.platform)
            return

        self._thread = threading.Thread(
            target=self._run_tap_loop, name="qimingxing-ptt", daemon=True
        )
        self._thread.start()

    def _emit_press(self) -> None:
        if self._pressed:
            return
        self._pressed = True
        if self._on_press is not None:
            try:
                self._on_press()
            except Exception as exc:
                logger.debug("PTT on_press callback error: %s", exc)

    def _emit_release(self) -> None:
        if not self._pressed:
            return
        self._pressed = False
        if self._on_release is not None:
            try:
                self._on_release()
            except Exception as exc:
                logger.debug("PTT on_release callback error: %s", exc)

    def _handle_event(self, event_type: int, keycode: int, flags: int, is_repeat: int) -> None:
        if self.spec.is_modifier_only:
            # FlagsChanged event code is 12 on Quartz
            if event_type == 12 and keycode in self.spec.key_codes:
                if (flags & self.spec.modifier_mask) != 0:
                    self._emit_press()
                else:
                    self._emit_release()
            return

        # Combo hotkey: KeyDown=10, KeyUp=11, FlagsChanged=12
        if event_type == 10 and is_repeat == 0 and keycode in self.spec.key_codes:
            if (flags & self.spec.modifier_mask) == self.spec.modifier_mask:
                self._emit_press()
        elif event_type == 11 and keycode in self.spec.key_codes:
            self._emit_release()
        elif event_type == 12 and self._pressed:
            if (flags & self.spec.modifier_mask) != self.spec.modifier_mask:
                self._emit_release()

    def _run_tap_loop(self) -> None:
        try:
            from Quartz import (  # type: ignore[import-not-found]
                CFMachPortCreateRunLoopSource,
                CFRunLoopAddSource,
                CFRunLoopGetCurrent,
                CFRunLoopRun,
                CFRunLoopStop,
                CGEventGetFlags,
                CGEventGetIntegerValueField,
                CGEventMaskBit,
                CGEventTapCreate,
                CGEventTapEnable,
                kCFRunLoopCommonModes,
                kCGEventFlagsChanged,
                kCGEventKeyDown,
                kCGEventKeyUp,
                kCGEventTapDisabledByTimeout,
                kCGEventTapOptionListenOnly,
                kCGHIDEventTap,
                kCGHeadInsertEventTap,
                kCGKeyboardEventAutorepeat,
                kCGKeyboardEventKeycode,
                kCGSessionEventTap,
            )
        except Exception as exc:
            logger.warning("Quartz 不可用，全局按住说话监听未启用: %s", exc)
            return

        mask = (
            CGEventMaskBit(kCGEventKeyDown)
            | CGEventMaskBit(kCGEventKeyUp)
            | CGEventMaskBit(kCGEventFlagsChanged)
        )

        def _callback(_proxy, event_type, event, _refcon):
            if event_type == kCGEventTapDisabledByTimeout and self._tap is not None:
                CGEventTapEnable(self._tap, True)
                return event
            keycode = int(CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode))
            is_repeat = int(CGEventGetIntegerValueField(event, kCGKeyboardEventAutorepeat))
            flags = int(CGEventGetFlags(event))
            self._handle_event(int(event_type), keycode, flags, is_repeat)
            return event

        tap = CGEventTapCreate(
            kCGHIDEventTap,
            kCGHeadInsertEventTap,
            kCGEventTapOptionListenOnly,
            mask,
            _callback,
            None,
        )
        if tap is None:
            tap = CGEventTapCreate(
                kCGSessionEventTap,
                kCGHeadInsertEventTap,
                kCGEventTapOptionListenOnly,
                mask,
                _callback,
                None,
            )
        if tap is None:
            logger.warning(
                "CGEventTapCreate 返回空：请在「系统设置 → 隐私与安全性 → 辅助功能/输入监控」授予权限后重启 Senses。"
            )
            return

        self._tap = tap
        source = CFMachPortCreateRunLoopSource(None, tap, 0)
        self._run_loop = CFRunLoopGetCurrent()
        CFRunLoopAddSource(self._run_loop, source, kCFRunLoopCommonModes)
        CGEventTapEnable(tap, True)
        logger.info("Global PTT hotkey listener active (%s)", self.spec.raw_spec)
        try:
            CFRunLoopRun()
        finally:
            self._emit_release()
            self._run_loop = None
            self._tap = None

    def stop(self) -> None:
        self._stop_event.set()
        self._emit_release()
        if self._run_loop is not None:
            try:
                from Quartz import CFRunLoopStop  # type: ignore[import-not-found]

                CFRunLoopStop(self._run_loop)
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None
