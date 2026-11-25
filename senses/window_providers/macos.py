from __future__ import annotations

from typing import Any, Callable, List, Mapping, Optional

from senses.window_providers.base import (
    ActiveWindowInfo,
    PermissionReport,
    WindowProvider,
    detect_self_window,
)

_SYSTEM_WINDOW_OWNERS = frozenset(
    {
        "window server",
        "dock",
        "control center",
        "notification center",
        "systemuiserver",
        "spotlight",
        "windowmanager",
        "wallpaper",
    }
)
_SELF_NATIVE_OWNERS = frozenset({"godot", "qimingxing", "启明星"})


def _pick_frontmost_from_quartz_windows(
    windows: List[Mapping[str, Any]],
) -> Optional[tuple[str, int]]:
    """Pick the front-most visible non-system, non-self layer-0 window from Quartz z-order."""
    for win in windows:
        try:
            layer = int(win.get("kCGWindowLayer", -1))
        except Exception:
            continue
        if layer != 0:
            continue

        try:
            alpha = float(win.get("kCGWindowAlpha", 1.0))
        except Exception:
            alpha = 1.0
        if alpha <= 0.0:
            continue

        bounds = win.get("kCGWindowBounds") or {}
        try:
            width = float(bounds.get("Width", 0.0))
            height = float(bounds.get("Height", 0.0))
        except Exception:
            continue
        if width < 50.0 or height < 50.0:
            continue

        owner = str(win.get("kCGWindowOwnerName") or "").strip()
        if not owner:
            continue
        owner_low = owner.lower()
        if owner_low in _SYSTEM_WINDOW_OWNERS or owner_low in _SELF_NATIVE_OWNERS:
            continue

        try:
            pid = int(win.get("kCGWindowOwnerPID", -1))
        except Exception:
            pid = -1
        if pid <= 0:
            continue
        return (owner, pid)
    return None


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


def _default_quartz_frontmost_fetcher() -> Optional[tuple[str, int, str]]:
    """Fetch live frontmost (app_name, pid, bundle_id) via WindowServer z-order (runloop-free)."""
    try:
        from Quartz import (  # type: ignore[import-not-found]
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListExcludeDesktopElements,
            kCGWindowListOptionOnScreenOnly,
        )
    except Exception:
        return _default_appkit_fetcher()

    options = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
    windows = CGWindowListCopyWindowInfo(options, kCGNullWindowID) or []
    picked = _pick_frontmost_from_quartz_windows(windows)
    if picked is not None:
        app_name, pid = picked
        bundle_id = ""
        try:
            from AppKit import NSRunningApplication  # type: ignore[import-not-found]

            running = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if running is not None:
                bundle_id = str(running.bundleIdentifier() or "")
        except Exception:
            bundle_id = ""
        return (app_name, pid, bundle_id)

    return _default_appkit_fetcher()


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


def _default_screen_permission_request() -> bool:
    try:
        from Quartz import CGRequestScreenCaptureAccess  # type: ignore[import-not-found]
    except Exception:
        return False
    try:
        return bool(CGRequestScreenCaptureAccess())
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
        screen_perm_requester: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._app_fetcher = app_fetcher or _default_quartz_frontmost_fetcher
        self._title_fetcher = title_fetcher or _default_quartz_window_title
        self._screen_perm_checker = screen_perm_checker or _default_screen_permission_check
        self._screen_perm_requester = screen_perm_requester or _default_screen_permission_request

    def request_screen_permission(self) -> bool:
        """Prompt/register this app in macOS Screen Recording TCC settings."""
        return bool(self._screen_perm_requester())

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
            is_self_window=detect_self_window(title, app_name, bundle_id),
        )

    def check_permission(self) -> PermissionReport:
        screen_ok = self._screen_perm_checker()
        window_ok = self._app_fetcher() is not None
        hints = []
        if not screen_ok:
            hints.append(
                "请在「系统设置 → 隐私与安全性 → 屏幕录制与系统录音」中允许「启明星 (Qimingxing)」（或当前终端）开启权限，授权后重启启明星生效。"
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
