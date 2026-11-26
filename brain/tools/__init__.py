from __future__ import annotations

from typing import Optional

from brain.config import AppConfig
from brain.tools.ask_user import AskUserTool
from brain.tools.base import Tool, ToolExecutionContext, ToolResult, ToolTraceStep
from brain.tools.bg_task_tools import (
    BgTaskCancelTool,
    BgTaskInspectTool,
    BgTaskListTool,
    BgTaskSubmitTool,
)
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
from brain.tools.image_search import ImageSearchTool, TavilyImageSearchProvider
from brain.tools.macos_apps import (
    MacOSBrowserTabsTool,
    MacOSCalendarCreateTool,
    MacOSCalendarListTool,
    MacOSMailDraftTool,
    MacOSMailSearchTool,
    MacOSNotesCreateTool,
    MacOSNotesSearchTool,
    MacOSRemindersCreateTool,
    MacOSRemindersListTool,
)
from brain.tools.mcp_client import MCPClientManager, MCPServerStatus, MCPToolAdapter
from brain.tools.mcp_oauth import FileTokenStorage, MCPOAuthCoordinator
from brain.tools.registry import ToolRegistry
from brain.tools.scheduler_tool import CancelTaskTool, ListTasksTool, ScheduleTaskTool
from brain.tools.shell_tool import ShellTool
from brain.tools.ui_control import OpenQimingxingUITool
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

    if config.tools.image_search.enabled:
        registry.register(ImageSearchTool(config.tools.image_search))

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
    registry.register(OpenQimingxingUITool())
    registry.register(ScheduleTaskTool())
    registry.register(ListTasksTool())
    registry.register(CancelTaskTool())
    if config.background_tasks.enabled:
        registry.register(BgTaskSubmitTool())
        registry.register(BgTaskListTool())
        registry.register(BgTaskCancelTool())
        registry.register(BgTaskInspectTool())
    registry.register(MacOSNotesSearchTool())
    registry.register(MacOSNotesCreateTool())
    registry.register(MacOSCalendarListTool())
    registry.register(MacOSCalendarCreateTool())
    registry.register(MacOSRemindersListTool())
    registry.register(MacOSRemindersCreateTool())
    registry.register(MacOSBrowserTabsTool())
    registry.register(MacOSMailSearchTool())
    registry.register(MacOSMailDraftTool())
    return registry


__all__ = [
    "AskUserTool",
    "BatchPathContextStore",
    "BgTaskCancelTool",
    "BgTaskInspectTool",
    "BgTaskListTool",
    "BgTaskSubmitTool",
    "CancelTaskTool",
    "ClipboardTool",
    "DeepSeekSearchProvider",
    "FinderGetSelectionTool",
    "FileTokenStorage",
    "ImageSearchTool",
    "InboxScanTool",
    "ListTasksTool",
    "MCPClientManager",
    "MCPOAuthCoordinator",
    "MCPServerStatus",
    "MCPToolAdapter",
    "MacOSBrowserTabsTool",
    "MacOSCalendarCreateTool",
    "MacOSCalendarListTool",
    "MacOSMailDraftTool",
    "MacOSMailSearchTool",
    "MacOSNotesCreateTool",
    "MacOSNotesSearchTool",
    "MacOSRemindersCreateTool",
    "MacOSRemindersListTool",
    "OpenQimingxingUITool",
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
    "TavilyImageSearchProvider",
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
