from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

from brain.tools.base import Tool, ToolExecutionContext, ToolResult
from protocol.models import RiskLevel


def _runner(ctx: ToolExecutionContext):
    return ctx.task_runner


_WORKER_RECIPE_MARKERS = (
    "\n步骤要求", "\n执行步骤", "\n操作步骤", "\n步骤：", "\n步骤:",
    "\n第一步", "\n1. ", "\n1、",
)
_TOOL_RECIPE_HINTS = (
    "doc_match_rename", "doc_generate", "sheet_transform", "pdf_ops",
    "table_path", "sequence_column", "match_columns", "output_dir",
)


def compact_dispatch_goal(goal: str) -> str:
    """Keep only the business objective when the Router writes a tool recipe.

    A natural request ("rename these invoices by the Excel 序号 and tell me
    which are missing") must reach the Worker as a goal, not as a step list
    naming tools and parameters. The first paragraph already carries the
    input/output locations and read-only constraints.
    """
    text = str(goal or "").strip()
    if not any(hint in text for hint in _TOOL_RECIPE_HINTS):
        return text
    for marker in _WORKER_RECIPE_MARKERS:
        index = text.find(marker)
        if 40 <= index <= 600:
            head = text[:index].strip()
            if len(head) >= 20:
                return head
    return text


class TaskDispatchTool(Tool):
    name = "task_dispatch"
    description = (
        "把多步、跨工具、耗时的任务派发给隔离后台 Worker 执行，立即返回 task_id。"
        "Router 不应直接执行 shell、整页抓取或多步写操作；复杂任务必须先走本工具。"
        "办公文件任务（表格/PDF/Word 核对、按表格序号重命名文件、PDF 合并拆分、生成报告）"
        "使用 doc_office 能力包；Worker 可使用 doc_match_rename 完成表格键值匹配重命名并生成 CSV 核对报告。"
        "用户通常只描述业务意图：请从上下文/附件推断输入目录，packs 填 doc_office，"
        "并把输入目录与期望输出目录一并放进 writable_paths（没有写入授权会被硬拒绝）。"
        "浏览器自动化使用 browser_auto 能力包：默认 browser_mode=isolated；"
        "只有确实需要用户已登录的主浏览器时才用 live，并必须提供 allowed_domains。"
    )
    default_risk: RiskLevel = "safe"
    parameters_schema = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "业务目标（1-2 句，含输入位置与期望结果）；"
                    "不要写分步步骤、工具名或参数配方，具体工具由 Worker 选择"
                ),
            },
            "title": {"type": "string", "description": "可选短标题"},
            "packs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "能力包：web_research / dev_ops / macos_office / media_studio / doc_office / browser_auto / mcp:<server>",
            },
            "writable_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "允许 Worker 写入的本地路径根目录；只读任务可留空",
            },
            "browser_mode": {
                "type": "string",
                "enum": ["isolated", "live"],
                "description": "浏览器模式；默认 isolated，live 需同时给出 allowed_domains",
            },
            "allowed_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "live 模式下允许接管的域名（必填且非空）",
            },
            "allow_downloads": {"type": "boolean"},
            "max_page_actions": {"type": "integer", "minimum": 1, "maximum": 200},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
        },
        "required": ["goal"],
    }

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return f"正在派发后台任务：{str(arguments.get('goal') or '')[:26]}…"

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        packs = arguments.get("packs")
        writable = arguments.get("writable_paths")
        if (
            (isinstance(packs, list) and "dev_ops" in [str(p) for p in packs])
            or (isinstance(writable, list) and any(str(p).strip() for p in writable))
            or str(arguments.get("browser_mode") or "") == "live"
        ):
            return "confirm"
        return "safe"

    def approval_scope_key(self, arguments: Dict[str, Any]) -> str | None:
        # Never cache a shell/write mandate approval for the whole session.
        return None

    def format_approval_summary(self, arguments: Dict[str, Any]) -> str:
        packs = arguments.get("packs") if isinstance(arguments.get("packs"), list) else []
        writable = arguments.get("writable_paths") if isinstance(arguments.get("writable_paths"), list) else []
        return (
            f"派发后台 Worker：{str(arguments.get('goal') or '')[:60]}"
            f"（能力包 {', '.join(str(p) for p in packs) or 'web_research'}"
            f"；写入根 {', '.join(str(p) for p in writable) or '无'}"
            f"；浏览器 {str(arguments.get('browser_mode') or 'isolated')}"
            f"{' / ' + ', '.join(str(d) for d in (arguments.get('allowed_domains') or [])) if arguments.get('browser_mode') == 'live' else ''}）"
        )

    def format_approval_details(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "goal": str(arguments.get("goal") or "")[:500],
            "packs": arguments.get("packs") or ["web_research"],
            "writable_paths": arguments.get("writable_paths") or [],
            "browser_mode": str(arguments.get("browser_mode") or "isolated"),
            "allowed_domains": arguments.get("allowed_domains") or [],
            "origin": "explicit_user",
        }

    async def execute(self, arguments: Dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        runner = _runner(ctx)
        if runner is None or not hasattr(runner, "submit"):
            return ToolResult(ok=False, output="后台 Worker 未就绪。", summary="Worker 未挂载", status="error")
        raw_goal = str(arguments.get("goal") or "").strip()
        goal = compact_dispatch_goal(raw_goal)
        if not goal:
            return ToolResult(ok=False, output="goal 不能为空。", summary="缺少目标", status="error")
        title = str(arguments.get("title") or goal[:40]).strip()[:120]
        packs = arguments.get("packs")
        if not isinstance(packs, list) or not packs:
            packs = ["web_research"]
        accepted = {
            "web_research", "dev_ops", "macos_office", "media_studio",
            "doc_office", "browser_auto",
        }
        clean_packs = [
            str(p).strip() for p in packs
            if str(p).strip() in accepted or str(p).strip().startswith("mcp:")
        ]
        if not clean_packs:
            clean_packs = ["web_research"]
        writable = arguments.get("writable_paths")
        if not isinstance(writable, list):
            writable = []
        writable_paths = [str(p).strip() for p in writable if str(p).strip()]
        if goal != raw_goal and writable_paths:
            # The recipe we dropped named the approved output folder; carry
            # just that fact into the compact business goal.
            goal = f"{goal}\n输出目录：{writable_paths[0]}"
        browser_mode = str(arguments.get("browser_mode") or "isolated").strip().lower()
        if browser_mode not in ("isolated", "live"):
            browser_mode = "isolated"
        allowed_domains = arguments.get("allowed_domains")
        if not isinstance(allowed_domains, list):
            allowed_domains = []
        allowed_domains = [str(d).strip().lower() for d in allowed_domains if str(d).strip()]
        if "browser_auto" in clean_packs and browser_mode == "live" and not allowed_domains:
            return ToolResult(
                ok=False,
                output="live 模式必须提供非空 allowed_domains（绑定允许接管的域名）。",
                summary="live 模式缺少域名白名单",
                status="error",
            )
        mandate = {
            "origin": "explicit_user",
            "allowed_packs": clean_packs,
            "writable_paths": writable_paths,
            "browser_mode": browser_mode if "browser_auto" in clean_packs else "isolated",
            "allowed_domains": allowed_domains if "browser_auto" in clean_packs else [],
            "allow_downloads": bool(arguments.get("allow_downloads", False)),
            "max_page_actions": int(arguments.get("max_page_actions") or 40),
        }
        record = await runner.submit(
            title=title,
            goal_prompt=goal,
            source_session_id=ctx.session_id,
            packs=clean_packs,
            mandate=mandate,
            origin="explicit_user",
        )
        return ToolResult(
            ok=True,
            output=(
                f"已派发隔离 Worker 任务 `{record.task_id}`（{record.title}），"
                f"能力包: {', '.join(clean_packs)}。完成后会汇报摘要与 Ref。"
            ),
            summary=f"已派发 Worker {record.task_id}",
            status="ok",
            metadata={"task_id": record.task_id, "packs": clean_packs},
        )


class TaskFollowupTool(Tool):
    name = "task_followup"
    description = "向正在运行的后台 Worker 投递补充约束或变更要求，不重启任务。"
    default_risk: RiskLevel = "safe"
    parameters_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "instruction": {"type": "string", "description": "简洁、可执行的新约束"},
        },
        "required": ["task_id", "instruction"],
    }

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return f"正在转告后台任务 {arguments.get('task_id', '')}…"

    async def execute(self, arguments: Dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        runner = _runner(ctx)
        if runner is None or not hasattr(runner, "followup"):
            return ToolResult(ok=False, output="后台 Worker 未就绪。", summary="Worker 未挂载", status="error")
        task_id = str(arguments.get("task_id") or "").strip()
        instruction = str(arguments.get("instruction") or "").strip()
        ok, msg = await runner.followup(task_id, instruction)
        return ToolResult(ok=ok, output=msg, summary="已转告补充要求" if ok else "转告失败",
                          status="ok" if ok else "error", metadata={"task_id": task_id})


class TaskInspectTool(Tool):
    name = "task_inspect"
    description = "查看后台任务状态、摘要或按 Ref 读取产物；用于追问任务细节。"
    default_risk: RiskLevel = "safe"
    parameters_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "ref_id": {"type": "string", "description": "可选产物 Ref，如 ref:task_42:report"},
        },
        "required": ["task_id"],
    }

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return f"正在查看后台任务 {arguments.get('task_id', '')}…"

    async def execute(self, arguments: Dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        runner = _runner(ctx)
        if runner is None:
            return ToolResult(ok=False, output="后台 Worker 未就绪。", summary="Worker 未挂载", status="error")
        task = await runner.get_task(str(arguments.get("task_id") or "").strip())
        if task is None:
            return ToolResult(ok=False, output="未找到该后台任务。", summary="任务不存在", status="error")
        ref_id = str(arguments.get("ref_id") or "").strip()
        if ref_id and ref_id in task.result_refs and task.artifact_path:
            try:
                content = Path(task.artifact_path).read_text(encoding="utf-8")
                return ToolResult(ok=True, output=content[:12000], summary="已读取任务产物",
                                  status="ok", metadata={"task_id": task.task_id, "ref_id": ref_id})
            except OSError:
                pass
        step_match = re.search(r"step[_-]?(\d+)", ref_id, re.IGNORECASE)
        if step_match:
            try:
                works = await runner.backend.list_work_history(limit=100)
                work = next((w for w in works if w.turn_id == task.task_id), None)
                if work is not None:
                    steps = json.loads(work.trace_json or "[]")
                    idx = int(step_match.group(1))
                    picked = [
                        s for s in steps
                        if int(s.get("step") or 0) == idx
                    ] if isinstance(steps, list) else []
                    if picked:
                        return ToolResult(
                            ok=True,
                            output=json.dumps(picked, ensure_ascii=False, indent=2)[:12000],
                            summary=f"已读取第 {idx} 步现场",
                            status="ok",
                            metadata={"task_id": task.task_id, "ref_id": ref_id},
                        )
            except Exception:
                pass
        parts = [
            f"任务 `{task.task_id}` | {task.title}",
            f"状态: {task.status} ({task.progress_pct}%)",
            f"步骤: {task.current_step}",
        ]
        if task.result_summary:
            parts.append(f"摘要: {task.result_summary}")
        if task.result_refs:
            parts.append("Refs: " + ", ".join(task.result_refs))
        if task.error_message:
            parts.append(f"错误: {task.error_message}")
        return ToolResult(ok=True, output="\n".join(parts), summary="已读取后台任务",
                          status="ok", metadata={"task_id": task.task_id})


class TaskCancelTool(Tool):
    name = "task_cancel"
    description = "取消指定的后台 Worker 任务。"
    default_risk: RiskLevel = "safe"
    parameters_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["task_id"],
    }

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return f"正在取消后台任务 {arguments.get('task_id', '')}…"

    async def execute(self, arguments: Dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        runner = _runner(ctx)
        if runner is None:
            return ToolResult(ok=False, output="后台 Worker 未就绪。", summary="Worker 未挂载", status="error")
        ok, msg = await runner.cancel(str(arguments.get("task_id") or "").strip())
        return ToolResult(ok=ok, output=msg, summary="已取消" if ok else "取消失败",
                          status="ok" if ok else "error")
