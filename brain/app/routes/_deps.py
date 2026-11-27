from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass
class RouteDeps:
    """Shared objects a route group needs, passed explicitly instead of
    captured from create_app's local scope."""

    hub: Any
    config: Any
    model_store: Any
    mcp: Any
    oauth: Any
    backend: Any
    task_runner: Any
    loopback_hosts: set[str]
    settings_config_path: Optional[Path]
    config_injected: bool
    desktop_command_runner: Any
    probe_transport: Any
