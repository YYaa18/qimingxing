from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Dict

from brain.memory.models import ScheduleKind, ScheduledTaskRecord, TaskType, new_task_id
from brain.scheduler import compute_next_run_at
from brain.tools.base import Tool, ToolExecutionContext, ToolResult
from protocol.models import RiskLevel


class ScheduleTaskTool(Tool):
    name = "schedule_task"
    description = (
        "创建定时提醒或周期定时摘要任务并持久化到 SQLite。"
        "纯提醒选 task_type='reminder'（零 Token）；需要调用模型总结工作足迹/日志时选 task_type='llm_summary'。"
    )
    default_risk: RiskLevel = "safe"
    parameters_schema = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "任务简短标题（例如：提醒喝水、每日工作足迹晚报）",
            },
            "task_type": {
                "type": "string",
                "enum": ["reminder", "llm_summary"],
                "description": "reminder 为零 Token 纯文本提醒；llm_summary 为到点调用快模型汇总",
            },
            "schedule_kind": {
                "type": "string",
                "enum": ["once", "daily", "interval"],
                "description": "once（一次性）、daily（每日固定时刻）、interval（固定周期）",
            },
            "schedule_expr": {
                "type": "string",
                "description": "once 支持 '+60s'/'5m'/'1h' 或 ISO 时间；daily 支持 '22:00'；interval 支持 '300s'/'10m'",
            },
            "message": {
                "type": "string",
                "description": "reminder 模式的提醒正文，或 llm_summary 模式的汇总指令",
            },
        },
        "required": ["title", "schedule_kind", "schedule_expr"],
    }

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return f"正在创建定时任务：{str(arguments.get('title') or '')[:24]}"

    async def execute(
        self,
        arguments: Dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        if ctx.memory_backend is None:
            return ToolResult(ok=False, output="未启用 SQLite 记忆库，无法创建定时任务。", summary="无记忆库", status="error")

        title = str(arguments.get("title") or "").strip()
        if not title:
            return ToolResult(ok=False, output="title 不能为空", summary="缺少标题", status="error")

        raw_type = str(arguments.get("task_type") or "reminder").strip().lower()
        task_type: TaskType = "llm_summary" if raw_type == "llm_summary" else "reminder"
        raw_kind = str(arguments.get("schedule_kind") or "once").strip().lower()
        if raw_kind not in ("once", "daily", "interval"):
            return ToolResult(ok=False, output=f"不支持的 schedule_kind: {raw_kind}", summary="周期类型无效", status="error")
        schedule_kind: ScheduleKind = raw_kind  # type: ignore[assignment]
        schedule_expr = str(arguments.get("schedule_expr") or "").strip()

        now = time.time()
        next_run = compute_next_run_at(schedule_kind, schedule_expr, now=now)
        if next_run is None:
            return ToolResult(
                ok=False,
                output=f"无法解析定时表达式 {schedule_expr!r}（once 示例 '+60s'，daily 示例 '22:00'，interval 示例 '5m'）",
                summary="定时表达式无效",
                status="error",
            )

        msg = str(arguments.get("message") or title).strip()
        payload = {"message": msg, "instruction": msg}
        rec = ScheduledTaskRecord(
            task_id=new_task_id(),
            title=title[:80],
            task_type=task_type,
            schedule_kind=schedule_kind,
            schedule_expr=schedule_expr,
            payload_json=json.dumps(payload, ensure_ascii=False),
            next_run_at=next_run,
            status="active",
            created_at=now,
        )
        await ctx.memory_backend.add_scheduled_task(rec)
        next_str = datetime.fromtimestamp(next_run).strftime("%Y-%m-%d %H:%M:%S")
        return ToolResult(
            ok=True,
            output=(
                f"已创建定时任务 {rec.task_id}：\n"
                f"- 标题：{rec.title}\n"
                f"- 模式：{rec.task_type}（{'零 Token' if rec.task_type == 'reminder' else '快模型摘要'}）\n"
                f"- 调度：{rec.schedule_kind} ({rec.schedule_expr})\n"
                f"- 下次触发时间：{next_str}"
            ),
            summary=f"已创建定时任务「{rec.title[:20]}」",
            metadata={"task_id": rec.task_id, "next_run_at": next_run},
        )


class ListTasksTool(Tool):
    name = "list_tasks"
    description = "列出当前已保存的定时提醒与定时摘要任务。"
    default_risk: RiskLevel = "safe"
    parameters_schema = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["active", "completed", "cancelled", "all"],
                "description": "按状态筛选，默认 active",
            }
        },
        "required": [],
    }

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return "正在查询定时任务列表…"

    async def execute(
        self,
        arguments: Dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        if ctx.memory_backend is None:
            return ToolResult(ok=False, output="未启用 SQLite 记忆库。", summary="无记忆库", status="error")
        raw_status = str(arguments.get("status") or "active").strip().lower()
        status_filter = None if raw_status == "all" else raw_status
        tasks = await ctx.memory_backend.list_scheduled_tasks(status=status_filter, limit=30)  # type: ignore[arg-type]
        if not tasks:
            return ToolResult(ok=True, output="当前没有匹配的定时任务。", summary="无定时任务")
        lines = []
        for t in tasks:
            next_str = datetime.fromtimestamp(t.next_run_at).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(
                f"- {t.task_id} | {t.status} | {t.task_type} | {t.schedule_kind}({t.schedule_expr}) | "
                f"下次: {next_str} | {t.title}"
            )
        return ToolResult(
            ok=True,
            output="\n".join(lines),
            summary=f"共找到 {len(tasks)} 条定时任务",
        )


class CancelTaskTool(Tool):
    name = "cancel_task"
    description = "取消指定的定时任务（支持按 task_id，或不传 ID 仅按 title 关键词匹配取消最近活跃任务）。"
    default_risk: RiskLevel = "safe"
    parameters_schema = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "要取消的任务 ID（如 task_xxx）",
            },
            "title_keyword": {
                "type": "string",
                "description": "当不知晓 task_id 时，按标题关键词匹配取消",
            },
        },
        "required": [],
    }

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return "正在取消定时任务…"

    async def execute(
        self,
        arguments: Dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        if ctx.memory_backend is None:
            return ToolResult(ok=False, output="未启用 SQLite 记忆库。", summary="无记忆库", status="error")

        task_id = str(arguments.get("task_id") or "").strip()
        keyword = str(arguments.get("title_keyword") or "").strip().lower()
        if not task_id:
            active = await ctx.memory_backend.list_scheduled_tasks(status="active", limit=20)
            matched = (
                [t for t in active if keyword in t.title.lower()]
                if keyword
                else active[-1:]
            )
            if not matched:
                return ToolResult(ok=False, output="未找到可取消的活跃定时任务。", summary="未找到匹配任务", status="error")
            task_id = matched[-1].task_id

        updated = await ctx.memory_backend.update_scheduled_task(task_id, status="cancelled")
        if updated is None:
            return ToolResult(ok=False, output=f"未找到定时任务 {task_id}", summary="任务不存在", status="error")
        return ToolResult(
            ok=True,
            output=f"已取消定时任务 {updated.task_id}（{updated.title}）。",
            summary=f"已取消「{updated.title[:20]}」",
        )
