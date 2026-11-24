from __future__ import annotations

from typing import Optional

from brain.config import AppConfig
from brain.tools.base import Tool, ToolExecutionContext, ToolResult, ToolTraceStep
from brain.tools.clipboard_tool import ClipboardTool
from brain.tools.executor import ToolExecutor, ToolLoopResult
from brain.tools.history_tool import WorkHistoryInspectTool
from brain.tools.mcp_client import MCPClientManager, MCPServerStatus, MCPToolAdapter
from brain.tools.registry import ToolRegistry
from brain.tools.scheduler_tool import CancelTaskTool, ListTasksTool, ScheduleTaskTool
from brain.tools.shell_tool import ShellTool
from brain.tools.web_search import (
    DeepSeekSearchProvider,
    SearchProvider,
    WebFetchTool,
    WebSearchTool,
)
from brain.utils import truncate_head_tail


def build_default_tool_registry(
    config: AppConfig,
    *,
    search_provider: Optional[SearchProvider] = None,
) -> ToolRegistry:
    """Build the default Stage 5 ToolRegistry according to AppConfig.tools."""
    registry = ToolRegistry()
    if not config.tools.enabled:
        return registry

    if config.tools.web_search.enabled:
        provider = search_provider or DeepSeekSearchProvider(config.tools.web_search)
        registry.register(WebSearchTool(config.tools.web_search, provider=provider))
        registry.register(WebFetchTool(config.tools.web_search, provider=provider))

    if config.tools.shell.enabled:
        registry.register(ShellTool(config.tools.shell))

    registry.register(ClipboardTool())
    registry.register(WorkHistoryInspectTool())
    registry.register(ScheduleTaskTool())
    registry.register(ListTasksTool())
    registry.register(CancelTaskTool())
    return registry


__all__ = [
    "CancelTaskTool",
    "ClipboardTool",
    "DeepSeekSearchProvider",
    "ListTasksTool",
    "MCPClientManager",
    "MCPServerStatus",
    "MCPToolAdapter",
    "ScheduleTaskTool",
    "SearchProvider",
    "ShellTool",
    "Tool",
    "ToolExecutionContext",
    "ToolExecutor",
    "ToolLoopResult",
    "ToolRegistry",
    "ToolResult",
    "ToolTraceStep",
    "WebFetchTool",
    "WebSearchTool",
    "WorkHistoryInspectTool",
    "build_default_tool_registry",
    "truncate_head_tail",
]
