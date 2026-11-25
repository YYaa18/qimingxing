from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

SELF_WINDOW_MARKERS = ("启明星", "Qimingxing")
SELF_PROCESS_MARKERS = (
    "启明星",
    "qimingxing",
    "godot",
    "com.qimingxing.desktop",
    "org.godotengine.godot",
)


def detect_self_window(
    window_title: str,
    app_name: str = "",
    process_name: str = "",
) -> bool:
    """Identify Qimingxing's own UI tab/window/process for anti-selfie context filtering."""
    for candidate in (window_title, app_name, process_name):
        if not candidate:
            continue
        lower = candidate.lower()
        markers = SELF_WINDOW_MARKERS if candidate is window_title else SELF_PROCESS_MARKERS
        if any(marker.lower() in lower for marker in markers):
            return True
    return False


@dataclass(frozen=True)
class ActiveWindowInfo:
    app_name: str
    process_name: str = ""
    window_title: str = ""
    is_self_window: bool = False


@dataclass(frozen=True)
class PermissionReport:
    window_meta: bool = True
    screen_capture: bool = True
    permission_hint: str = ""


class WindowProvider(ABC):
    """Thin OS abstraction for active window metadata & system permission checks."""

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Canonical platform identifier ('macos' or 'windows')."""

    @abstractmethod
    def get_active_window(self) -> Optional[ActiveWindowInfo]:
        """Return the current foreground application and window title."""

    @abstractmethod
    def check_permission(self) -> PermissionReport:
        """Inspect OS-level privacy permissions and return actionable guidance if missing."""
