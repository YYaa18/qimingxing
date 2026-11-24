from __future__ import annotations

from typing import Any, Dict

from brain.tools.base import Tool, ToolExecutionContext, ToolResult
from protocol.models import RiskLevel


class WorkHistoryInspectTool(Tool):
    name = "work_history_inspect"
    description = (
        "按需翻看启明星过往多步工具任务的归档执行轨迹与步骤细节（work_history）。"
        "仅当用户明确追问“刚才怎么操作的 / 哪一步出错了 / 查看工作记录”时调用。"
    )
    default_risk: RiskLevel = "safe"
    parameters_schema = {
        "type": "object",
        "properties": {
            "work_id": {
                "type": "string",
                "description": "可选，指定工作记录 ID；不传则列出最近几条记录详情",
            },
            "limit": {
                "type": "integer",
                "description": "返回最近条数（默认 3，最大 10）",
                "minimum": 1,
                "maximum": 10,
            },
        },
        "required": [],
    }

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return "正在查阅归档工作记录…"

    async def execute(
        self,
        arguments: Dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        if ctx.memory_backend is None:
            return ToolResult(
                ok=False,
                output="当前未启用 SQLite 记忆库，暂无可翻看的工作历史。",
                summary="未启用工作历史",
                status="error",
            )
        work_id = str(arguments.get("work_id") or "").strip()
        if work_id:
            rec = await ctx.memory_backend.get_work_history(work_id)
            if rec is None:
                return ToolResult(ok=False, output=f"未找到工作记录 {work_id}", summary="未找到记录", status="error")
            return ToolResult(
                ok=True,
                output=(
                    f"work_id={rec.work_id} status={rec.status} steps={rec.step_count}\n"
                    f"目的: {rec.goal}\n"
                    f"结果: {rec.result_summary}\n"
                    f"轨迹明细 JSON:\n{rec.trace_json}"
                ),
                summary=f"已读取记录 {rec.work_id}",
            )

        limit = min(10, max(1, int(arguments.get("limit") or 3)))
        rows = await ctx.memory_backend.list_work_history(limit=limit)
        if not rows:
            return ToolResult(ok=True, output="暂无归档的多步工作历史记录。", summary="暂无工作历史")
        parts = []
        for r in rows:
            parts.append(
                f"- [{r.work_id}] ({r.status}, {r.step_count} 步) 目的: {r.goal}\n"
                f"  结果: {r.result_summary}\n"
                f"  步骤轨迹: {r.trace_json[:800]}"
            )
        return ToolResult(
            ok=True,
            output="\n".join(parts),
            summary=f"已读取最近 {len(rows)} 条工作历史",
        )
