from __future__ import annotations

import sys
from typing import Optional

from senses.window_providers.base import WindowProvider
from senses.window_providers.macos import MacOSWindowProvider
from senses.window_providers.windows import WindowsWindowProvider


def create_window_provider(platform: Optional[str] = None) -> WindowProvider:
    """Instantiate the OS-specific WindowProvider based on sys.platform."""
    target = (platform or sys.platform).lower()
    if target.startswith("darwin") or target == "macos":
        return MacOSWindowProvider()
    if target.startswith("win") or target == "windows":
        return WindowsWindowProvider()
    raise NotImplementedError(
        f"Unsupported desktop platform '{target}'. Qimingxing MVP 2.0 supports macOS and Windows."
    )
