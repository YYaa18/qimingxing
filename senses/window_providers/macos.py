from __future__ import annotations

from typing import Any, Callable, Optional

from senses.window_providers.base import (
    ActiveWindowInfo,
    PermissionReport,
    WindowProvider,
    detect_self_window,
)


def _default_appkit_fetcher() -> Optional[tuple[str, int, str]]:
    """Fetch (app_name, pid, bundle_id) of the frontmost macOS application."""
    try:
        from AppKit import NSWorkspace  # type: ignore[import-not-found]
    except Exception:
        return None

    ws = NSWorkspace.sharedWorkspace()
    app = ws.frontmostApplication()
    if app is None:
        return None
    app_name = str(app.localizedName() or "")
    pid = int(app.processIdentifier())
    bundle_id = str(app.bundleIdentifier() or "")
    return (app_name, pid, bundle_id)


def _default_quartz_window_title(pid: int) -> str:
    """Fetch top-most visible window title belonging to `pid` via Quartz."""
    try:
        from Quartz import (  # type: ignore[import-not-found]
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListExcludeDesktopElements,
            kCGWindowListOptionOnScreenOnly,
        )
    except Exception:
        return ""

    options = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
    windows = CGWindowListCopyWindowInfo(options, kCGNullWindowID) or []
    for win in windows:
        if int(win.get("kCGWindowOwnerPID", -1)) != pid:
            continue
        if int(win.get("kCGWindowLayer", 0)) != 0:
            continue
        title = str(win.get("kCGWindowName") or "").strip()
        if title:
            return title
    return ""


def _default_screen_permission_check() -> bool:
    try:
        from Quartz import CGPreflightScreenCaptureAccess  # type: ignore[import-not-found]
    except Exception:
        return False
    try:
        return bool(CGPreflightScreenCaptureAccess())
    except Exception:
        return False


class MacOSWindowProvider(WindowProvider):
    """macOS active window & screen recording permission adapter."""

    def __init__(
        self,
        *,
        app_fetcher: Optional[Callable[[], Optional[tuple[str, int, str]]]] = None,
        title_fetcher: Optional[Callable[[int], str]] = None,
        screen_perm_checker: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._app_fetcher = app_fetcher or _default_appkit_fetcher
        self._title_fetcher = title_fetcher or _default_quartz_window_title
        self._screen_perm_checker = screen_perm_checker or _default_screen_permission_check

    @property
    def platform_name(self) -> str:
        return "macos"

    def get_active_window(self) -> Optional[ActiveWindowInfo]:
        info = self._app_fetcher()
        if info is None:
            return None
        app_name, pid, bundle_id = info
        if not app_name:
            return None
        title = self._title_fetcher(pid)
        return ActiveWindowInfo(
            app_name=app_name,
            process_name=bundle_id or app_name,
            window_title=title,
            is_self_window=detect_self_window(title),
        )

    def check_permission(self) -> PermissionReport:
        screen_ok = self._screen_perm_checker()
        window_ok = self._app_fetcher() is not None
        hints = []
        if not screen_ok:
            hints.append(
                "请在「系统设置 → 隐私与安全性 → 屏幕录制与系统录音」中允许当前终端/Python 开启权限。"
            )
        if not window_ok:
            hints.append(
                "请在「系统设置 → 隐私与安全性 → 辅助功能」中授予前台应用读取权限。"
            )
        return PermissionReport(
            window_meta=window_ok,
            screen_capture=screen_ok,
            permission_hint=" ".join(hints),
        )
