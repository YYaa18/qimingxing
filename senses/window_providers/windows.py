from __future__ import annotations

import ctypes
import ctypes.wintypes
from pathlib import Path
from typing import Callable, Optional

from senses.window_providers.base import (
    ActiveWindowInfo,
    PermissionReport,
    WindowProvider,
    detect_self_window,
)

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _default_win32_foreground_info() -> Optional[tuple[str, str, str]]:
    """Query foreground window title & executable name on Windows via stdlib ctypes."""
    if not hasattr(ctypes, "windll"):
        return None
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        length = user32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(max(length + 1, 1))
        user32.GetWindowTextW(hwnd, title_buf, length + 1)
        window_title = title_buf.value.strip()

        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        process_name = ""
        app_name = ""
        if pid.value:
            handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if handle:
                try:
                    exe_buf = ctypes.create_unicode_buffer(1024)
                    size = ctypes.wintypes.DWORD(1024)
                    if kernel32.QueryFullProcessImageNameW(
                        handle, 0, exe_buf, ctypes.byref(size)
                    ):
                        exe_path = exe_buf.value
                        process_name = Path(exe_path).name
                        app_name = Path(exe_path).stem
                finally:
                    kernel32.CloseHandle(handle)

        if not app_name:
            app_name = window_title or "Windows App"
        return (app_name, process_name or app_name, window_title)
    except Exception:
        return None


class WindowsWindowProvider(WindowProvider):
    """Windows active window & permission adapter via Win32 API."""

    def __init__(
        self,
        *,
        foreground_fetcher: Optional[Callable[[], Optional[tuple[str, str, str]]]] = None,
    ) -> None:
        self._foreground_fetcher = foreground_fetcher or _default_win32_foreground_info

    @property
    def platform_name(self) -> str:
        return "windows"

    def get_active_window(self) -> Optional[ActiveWindowInfo]:
        info = self._foreground_fetcher()
        if info is None:
            return None
        app_name, process_name, window_title = info
        if not app_name and not window_title:
            return None
        return ActiveWindowInfo(
            app_name=app_name,
            process_name=process_name,
            window_title=window_title,
            is_self_window=detect_self_window(window_title),
        )

    def check_permission(self) -> PermissionReport:
        # Standard desktop user sessions on Windows expose foreground title and GDI/DXGI capture without prompt;
        # UAC elevated windows cannot be inspected by non-elevated processes.
        return PermissionReport(
            window_meta=True,
            screen_capture=True,
            permission_hint="",
        )
