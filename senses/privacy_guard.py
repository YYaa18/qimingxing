from __future__ import annotations

import re
from typing import Iterable, Optional, Tuple

from brain.config import DEFAULT_BLOCKED_APPS, DEFAULT_BLOCKED_TITLE_KEYWORDS
from protocol.models import ContextWindowChangedPayload, PrivacyMode
from senses.window_providers.base import ActiveWindowInfo

MASKED_PLACEHOLDER = "[隐私窗口已隐藏]"


class PrivacyGuard:
    """Edge privacy guard enforcing 3-tier mode and local app/title blacklist."""

    def __init__(
        self,
        mode: PrivacyMode = "meta_only",
        blocked_apps: Optional[Iterable[str]] = None,
        blocked_title_keywords: Optional[Iterable[str]] = None,
    ) -> None:
        self.mode: PrivacyMode = mode
        apps = list(blocked_apps) if blocked_apps is not None else list(DEFAULT_BLOCKED_APPS)
        keywords = (
            list(blocked_title_keywords)
            if blocked_title_keywords is not None
            else list(DEFAULT_BLOCKED_TITLE_KEYWORDS)
        )
        self._app_patterns = [
            re.compile(re.escape(item), re.IGNORECASE) for item in apps if item.strip()
        ]
        self._title_patterns = [
            re.compile(re.escape(item), re.IGNORECASE) for item in keywords if item.strip()
        ]

    def set_mode(self, mode: PrivacyMode) -> None:
        self.mode = mode

    def allows_window_reporting(self) -> bool:
        return self.mode != "off"

    def is_blocked(self, app_name: str, window_title: str = "") -> bool:
        for pat in self._app_patterns:
            if pat.search(app_name or ""):
                return True
        for pat in self._title_patterns:
            if pat.search(window_title or "") or pat.search(app_name or ""):
                return True
        return False

    def sanitize_window(self, info: ActiveWindowInfo) -> ContextWindowChangedPayload:
        """Transform raw foreground window info into protocol payload, masking sensitive windows."""
        if self.is_blocked(info.app_name, info.window_title):
            return ContextWindowChangedPayload(
                app_name=MASKED_PLACEHOLDER,
                process_name="",
                window_title=MASKED_PLACEHOLDER,
                scene="privacy",
                privacy_masked=True,
                is_self_window=False,
            )

        scene = _infer_scene(info.app_name, info.window_title)
        return ContextWindowChangedPayload(
            app_name=info.app_name,
            process_name=info.process_name,
            window_title=info.window_title,
            scene=scene,
            privacy_masked=False,
            is_self_window=info.is_self_window,
        )

    def can_capture(self, current_window: Optional[ActiveWindowInfo]) -> Tuple[bool, str]:
        """Check whether single-shot screen capture is permitted right now."""
        if self.mode != "full_on_demand":
            return False, "permission_denied"
        if current_window is not None and self.is_blocked(
            current_window.app_name, current_window.window_title
        ):
            return False, "privacy_blocked"
        return True, ""


def _infer_scene(app_name: str, window_title: str) -> str:
    combined = f"{app_name} {window_title}".lower()
    if any(k in combined for k in ("code", "pycharm", "intellij", "xcode", "terminal", "iterm", "warp")):
        return "coding"
    if any(k in combined for k in ("chrome", "safari", "edge", "firefox", "arc")):
        return "browser"
    if any(k in combined for k in ("notion", "obsidian", "word", "docs", "pages")):
        return "office"
    return "desktop"
