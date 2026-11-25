from __future__ import annotations

from typing import Any, Dict

from brain.tools.base import Tool, ToolExecutionContext, ToolResult
from protocol.models import RiskLevel


class BgTaskSubmitTool(Tool):
    """Submit a non-blocking multi-step background task and return control immediately."""

    name = "bg_task_submit"
    description = (
        "将耗时多步分析、资料汇总、跨应用检索或长文报告生成任务提交到后台异步长任务引擎执行，"
        "立即释放前台对话输入（1 秒内返回任务 ID），完成后主动发通知并交付报告产物。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "简短清晰的任务标题（如：整理今日浏览器标签页并生成笔记草稿）",
            },
            "goal_prompt": {
                "type": "string",
                "description": "详细的后台执行目标与步骤要求",
            },
        },
        "required": ["title", "goal_prompt"],
    }
    default_risk: RiskLevel = "safe"

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        return "safe"

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        title = str(arguments.get("title") or "后台任务").strip()
        return f"正在派发后台长任务：{title[:30]}…"

    async def execute(
        self,
        arguments: Dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        runner = ctx.task_runner
        if runner is None:
            return ToolResult(
                ok=False,
                output="后台任务引擎当前未挂载，无法派发异步任务。",
                summary="后台任务引擎未就绪",
                status="error",
            )
        title = str(arguments.get("title") or "").strip()
        goal_prompt = str(arguments.get("goal_prompt") or "").strip()
        if not title or not goal_prompt:
            return ToolResult(
                ok=False,
                output="请提供完整的任务 title 与 goal_prompt。",
                summary="参数缺失",
                status="error",
            )
        record = await runner.submit(
            title=title,
            goal_prompt=goal_prompt,
            source_session_id=ctx.session_id,
        )
        return ToolResult(
            ok=True,
            output=(
                f"已派发后台任务 `{record.task_id}`（{record.title}），当前状态: {record.status}。"
                "前台输入已释放，完成后我将主动发出通知并交付产物报告。"
            ),
            summary=f"已派发后台任务 {record.task_id}",
            status="ok",
            metadata={"task_id": record.task_id, "status": record.status},
        )


class BgTaskListTool(Tool):
    """List recent background long tasks and their progress."""

    name = "bg_task_list"
    description = "查看后台异步长任务队列、执行进度及近期完成记录。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": [
                    "queued",
                    "running",
                    "waiting_approval",
                    "completed",
                    "failed",
                    "cancelled",
                    "interrupted",
                ],
                "description": "可选，按状态筛选",
            },
            "limit": {
                "type": "integer",
                "description": "返回数量上限，默认 10",
            },
        },
    }
    default_risk: RiskLevel = "safe"

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        return "safe"

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return "正在查询后台任务列表…"

    async def execute(
        self,
        arguments: Dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        runner = ctx.task_runner
        if runner is None:
            return ToolResult(
                ok=False,
                output="后台任务引擎当前未挂载。",
                summary="后台任务引擎未就绪",
                status="error",
            )
        status = arguments.get("status")
        limit = max(1, min(50, int(arguments.get("limit") or 10)))
        tasks = await runner.list_tasks(status=status, limit=limit)
        if not tasks:
            return ToolResult(
                ok=True,
                output="当前没有匹配的后台任务记录。",
                summary="无后台任务",
                status="ok",
            )
        lines = []
        for t in tasks:
            line = f"- `{t.task_id}` | {t.title} | 状态: {t.status} ({t.progress_pct}%)"
            if t.current_step:
                line += f" | 步骤: {t.current_step}"
            if t.artifact_path:
                line += f" | 产物: {t.artifact_path}"
            lines.append(line)
        return ToolResult(
            ok=True,
            output="\n".join(lines),
            summary=f"共 {len(tasks)} 条后台任务",
            status="ok",
            metadata={"count": len(tasks)},
        )


class BgTaskCancelTool(Tool):
    """Cancel a queued, running, or approval-waiting background task."""

    name = "bg_task_cancel"
    description = "取消指定的后台异步长任务。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "要取消的后台任务 ID（如 bg_xxxxxxxxxxxx）",
            }
        },
        "required": ["task_id"],
    }
    default_risk: RiskLevel = "safe"

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        return "safe"

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return f"正在取消后台任务 {arguments.get('task_id', '')}…"

    async def execute(
        self,
        arguments: Dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        runner = ctx.task_runner
        if runner is None:
            return ToolResult(
                ok=False,
                output="后台任务引擎当前未挂载。",
                summary="后台任务引擎未就绪",
                status="error",
            )
        task_id = str(arguments.get("task_id") or "").strip()
        if not task_id:
            return ToolResult(
                ok=False,
                output="请提供 task_id。",
                summary="缺少 task_id",
                status="error",
            )
        ok, msg = await runner.cancel(task_id)
        return ToolResult(
            ok=ok,
            output=msg,
            summary="已取消后台任务" if ok else "取消后台任务失败",
            status="ok" if ok else "error",
            metadata={"task_id": task_id},
        )


class BgTaskInspectTool(Tool):
    """Inspect full details, summary, error, and artifact path of a background task."""

    name = "bg_task_inspect"
    description = "查看单个后台异步长任务的详细信息、执行摘要、错误信息与交付产物路径。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "后台任务 ID",
            }
        },
        "required": ["task_id"],
    }
    default_risk: RiskLevel = "safe"

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        return "safe"

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return f"正在查看后台任务 {arguments.get('task_id', '')} 详情…"

    async def execute(
        self,
        arguments: Dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        runner = ctx.task_runner
        if runner is None:
            return ToolResult(
                ok=False,
                output="后台任务引擎当前未挂载。",
                summary="后台任务引擎未就绪",
                status="error",
            )
        task_id = str(arguments.get("task_id") or "").strip()
        task = await runner.get_task(task_id)
        if task is None:
            return ToolResult(
                ok=False,
                output=f"未找到后台任务 `{task_id}`。",
                summary="任务不存在",
                status="error",
            )
        parts = [
            f"任务 ID: `{task.task_id}`",
            f"标题: {task.title}",
            f"状态: {task.status} ({task.progress_pct}%)",
        ]
        if task.current_step:
            parts.append(f"当前步骤: {task.current_step}")
        if task.result_summary:
            parts.append(f"结果摘要:\n{task.result_summary}")
        if task.artifact_path:
            parts.append(f"产物路径: {task.artifact_path}")
        if task.error_message:
            parts.append(f"错误信息: {task.error_message}")
        return ToolResult(
            ok=True,
            output="\n".join(parts),
            summary=f"任务 {task.task_id}: {task.status}",
            status="ok",
            metadata={"task_id": task.task_id, "status": task.status},
        )
