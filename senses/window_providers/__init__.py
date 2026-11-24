from __future__ import annotations

from senses.window_providers.base import ActiveWindowInfo, PermissionReport, WindowProvider
from senses.window_providers.factory import create_window_provider
from senses.window_providers.macos import MacOSWindowProvider
from senses.window_providers.windows import WindowsWindowProvider

__all__ = [
    "ActiveWindowInfo",
    "MacOSWindowProvider",
    "PermissionReport",
    "WindowProvider",
    "WindowsWindowProvider",
    "create_window_provider",
]
