from __future__ import annotations

from typing import Optional

from brain.config import AppConfig
from brain.tools.ask_user import AskUserTool
from brain.tools.base import Tool, ToolExecutionContext, ToolResult, ToolTraceStep
from brain.tools.clipboard_tool import ClipboardTool
from brain.tools.desktop_io import (
    BatchPathContextStore,
    FinderGetSelectionTool,
    InboxScanTool,
    actions_for_media_type,
    build_path_manifest,
    execute_desktop_action,
    infer_media_type,
    is_sensitive_path,
)
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
    registry.register(FinderGetSelectionTool(
        max_paths=config.batch_context.max_paths,
        max_tree_depth=config.batch_context.max_tree_depth,
    ))
    registry.register(InboxScanTool(config))
    registry.register(WorkHistoryInspectTool())
    registry.register(AskUserTool())
    registry.register(ScheduleTaskTool())
    registry.register(ListTasksTool())
    registry.register(CancelTaskTool())
    return registry


__all__ = [
    "AskUserTool",
    "BatchPathContextStore",
    "CancelTaskTool",
    "ClipboardTool",
    "DeepSeekSearchProvider",
    "FinderGetSelectionTool",
    "InboxScanTool",
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
    "actions_for_media_type",
    "build_default_tool_registry",
    "build_path_manifest",
    "execute_desktop_action",
    "infer_media_type",
    "is_sensitive_path",
    "truncate_head_tail",
]
