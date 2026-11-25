from __future__ import annotations

from typing import Any, Dict, Optional

from brain.tools.base import Tool, ToolExecutionContext, ToolResult
from protocol.models import RiskLevel

_ACTIONS = {
    "open_workspace": ("打开工作区", "已打开工作区。"),
    "open_settings": ("打开设置", "已打开设置。"),
    "open_quick_entry": ("唤起快速输入框", "已唤起快速输入框。"),
    "detach_workspace": ("拆分工作区", "已把工作区拆到独立窗口。"),
    "close_workspace": ("收起工作区", "已收起工作区。"),
}


class OpenQimingxingUITool(Tool):
    """Lets the assistant drive its own native shell instead of guessing filesystem paths."""

    name = "open_qimingxing_ui"
    description = (
        "打开或切换启明星自己的界面组件。用户说“打开工作区 / 看看我的工作区 / "
        "打开设置 / 唤起输入框”时，必须调用本工具；不要用 shell、Finder、文件系统或 "
        "其他应用去推断工作区路径。action 取值：open_workspace（在聊天框右侧展开简易工作区）、"
        "detach_workspace（把工作区拆分为独立窗口）、close_workspace（收起侧翼）、"
        "open_settings（设置，可选 tab）、"
        "open_quick_entry（快速输入框）。"
    )
    default_risk: RiskLevel = "safe"
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "open_workspace",
                    "open_settings",
                    "open_quick_entry",
                    "detach_workspace",
                    "close_workspace",
                ],
                "description": "要打开的启明星界面组件。",
            },
            "tab": {
                "type": "string",
                "description": "可选，仅 open_settings 使用，例如 general / routing。",
            },
        },
        "required": ["action"],
    }

    def approval_scope_key(self, arguments: Dict[str, Any]) -> Optional[str]:
        return None

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        action = str(arguments.get("action") or "")
        label = _ACTIONS.get(action, ("打开界面", ""))[0]
        return f"正在{label}…"

    def format_approval_summary(self, arguments: Dict[str, Any]) -> str:
        action = str(arguments.get("action") or "")
        return _ACTIONS.get(action, ("打开界面", ""))[0]

    async def execute(
        self,
        arguments: Dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        action = str(arguments.get("action") or "").strip()
        if action not in _ACTIONS:
            return ToolResult(
                ok=False,
                output=f"不支持的 action：{action}",
                summary="界面操作无效",
                status="error",
            )

        args: Dict[str, Any] = {}
        tab = str(arguments.get("tab") or "").strip()
        if action == "open_settings" and tab:
            args["tab"] = tab

        ok, error = await ctx.request_ui_command(action, args)
        label, success_output = _ACTIONS[action]
        if ok:
            return ToolResult(
                ok=True,
                output=success_output,
                summary=label,
                metadata={"action": action},
            )
        return ToolResult(
            ok=False,
            output=f"{label}失败：{error or '界面不可用'}",
            summary=f"{label}失败",
            status="error",
            metadata={"action": action, "error": error},
        )
