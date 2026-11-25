from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from brain.config import AppConfig
from brain.llm_router import LLMRouter, LLMUsage
from brain.memory.backend import MemoryBackend
from brain.memory.models import (
    BackgroundTaskRecord,
    BackgroundTaskStatus,
    UsageLedgerRecord,
    WorkHistoryRecord,
    new_bg_task_id,
    new_notice_id,
    new_work_id,
)
from brain.tools.base import ToolExecutionContext
from brain.tools.executor import ToolExecutor
from brain.tools.registry import ToolRegistry
from protocol.models import (
    OutputArtifactCapsulePayload,
    OutputProactiveNoticePayload,
    RiskLevel,
    TaskStatusPayload,
)

logger = logging.getLogger("qimingxing.task_runner")

TaskStatusCallback = Callable[[TaskStatusPayload], Awaitable[None]]
NoticeCallback = Callable[[OutputProactiveNoticePayload], Awaitable[None]]
ArtifactCapsuleCallback = Callable[[OutputArtifactCapsulePayload], Awaitable[None]]
SoftAlertCallback = Callable[[str], Awaitable[None]]
ApprovalCallback = Callable[
    [str, str, RiskLevel, str, Dict[str, Any], int], Awaitable[tuple[bool, bool]]
]
AskUserCallback = Callable[[str, str, List[str], int], Awaitable[Optional[str]]]

_BG_TASK_SYSTEM_PROMPT = (
    "你是启明星（Qimingxing）后台异步长任务执行引擎。\n"
    "当前运行在独立隔离上下文中，主对话不受阻塞。\n"
    "请严谨调用可用工具收集与核实信息，禁止编造未观测的结果或路径；\n"
    "如果遇到需要用户确认的操作，请正常发起工具调用以触发原生审批弹窗。\n"
    "最终输出结构化、清晰简洁的中文结论报告。"
)


class BackgroundTaskRunner:
    """Lightweight asyncio pool for non-blocking, isolated multi-step background tasks."""

    def __init__(
        self,
        backend: MemoryBackend,
        llm_router: LLMRouter,
        config: AppConfig,
        tool_registry: ToolRegistry,
        *,
        artifact_dir: Optional[Path] = None,
        on_task_status: Optional[TaskStatusCallback] = None,
        on_notice: Optional[NoticeCallback] = None,
        on_artifact_capsule: Optional[ArtifactCapsuleCallback] = None,
        on_soft_alert: Optional[SoftAlertCallback] = None,
        on_request_approval: Optional[ApprovalCallback] = None,
        on_ask_user: Optional[AskUserCallback] = None,
    ) -> None:
        self.backend = backend
        self.llm_router = llm_router
        self.config = config
        self.tool_registry = tool_registry
        self.artifact_dir = Path(
            artifact_dir
            if artifact_dir is not None
            else Path(config.memory.sqlite_path).expanduser().resolve().parent / "artifacts"
        )
        self.on_task_status = on_task_status
        self.on_notice = on_notice
        self.on_artifact_capsule = on_artifact_capsule
        self.on_soft_alert = on_soft_alert
        self.on_request_approval = on_request_approval
        self.on_ask_user = on_ask_user

        max_conc = max(1, int(config.background_tasks.max_concurrency))
        self._semaphore = asyncio.Semaphore(max_conc)
        self._tasks: Dict[str, asyncio.Task[None]] = {}
        self._cancel_events: Dict[str, asyncio.Event] = {}
        self._session_approved_tools: set[str] = set()

    async def initialize(self) -> int:
        """Mark leftover active tasks from an unexpected restart as interrupted."""
        count = await self.backend.mark_unfinished_background_tasks_interrupted()
        if count > 0:
            logger.warning("Marked %d unfinished background tasks as interrupted on startup", count)
        return count

    async def shutdown(self) -> None:
        """Cancel all in-flight background tasks cleanly on process shutdown."""
        for ev in self._cancel_events.values():
            ev.set()
        pending = [t for t in self._tasks.values() if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._cancel_events.clear()

    def active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if not t.done())

    async def submit(
        self,
        *,
        title: str,
        goal_prompt: str,
        source_session_id: str = "",
    ) -> BackgroundTaskRecord:
        task_id = new_bg_task_id()
        now = time.time()
        clean_title = (title or "后台分析任务").strip()[:120]
        record = BackgroundTaskRecord(
            task_id=task_id,
            title=clean_title,
            goal_prompt=goal_prompt.strip(),
            source_session_id=source_session_id,
            status="queued",
            progress_pct=0,
            current_step="已加入后台队列，等待执行…",
            slot_used="bg_task",
            created_at=now,
        )
        await self.backend.add_background_task(record)
        cancel_ev = asyncio.Event()
        self._cancel_events[task_id] = cancel_ev
        bg_task = asyncio.create_task(self._run_task(record, cancel_ev))
        self._tasks[task_id] = bg_task
        await self._emit_status(record)
        return record

    async def cancel(self, task_id: str) -> tuple[bool, str]:
        rec = await self.backend.get_background_task(task_id)
        if rec is None:
            return False, f"未找到后台任务 `{task_id}`。"
        if rec.status in ("completed", "failed", "cancelled", "interrupted"):
            return False, f"后台任务 `{task_id}` 当前状态为 {rec.status}，无需取消。"

        ev = self._cancel_events.get(task_id)
        if ev is not None:
            ev.set()
        task = self._tasks.get(task_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        updated = await self.backend.update_background_task(
            task_id,
            status="cancelled",
            current_step="用户已取消",
            finished_at=time.time(),
        )
        if updated is not None:
            await self._emit_status(updated)
        return True, f"已取消后台任务 `{task_id}`。"

    async def get_task(self, task_id: str) -> Optional[BackgroundTaskRecord]:
        return await self.backend.get_background_task(task_id)

    async def list_tasks(
        self,
        *,
        status: Optional[BackgroundTaskStatus] = None,
        limit: int = 20,
    ) -> List[BackgroundTaskRecord]:
        return await self.backend.list_background_tasks(status=status, limit=limit)

    async def _emit_status(self, rec: BackgroundTaskRecord) -> None:
        if self.on_task_status is None:
            return
        payload = TaskStatusPayload(
            task_id=rec.task_id,
            title=rec.title,
            status=rec.status,
            progress_pct=rec.progress_pct,
            current_step=rec.current_step,
            active_count=self.active_count(),
            result_summary=rec.result_summary,
            artifact_path=rec.artifact_path,
            error_message=rec.error_message,
        )
        try:
            await self.on_task_status(payload)
        except Exception:
            logger.debug("Failed emitting task.status for %s", rec.task_id)

    def _build_sub_registry(self) -> ToolRegistry:
        sub = ToolRegistry()
        for tool in self.tool_registry.list_tools():
            if tool.name == "bg_task_submit":
                continue
            sub.register(tool)
        return sub

    def _resolve_bg_profile_name(self) -> Optional[str]:
        try:
            cur = self.llm_router.get_profile()
            if cur.provider == "mock":
                return None
        except Exception:
            pass
        if self.config.has_explicit_routing:
            return "@slot:fast_utility"
        return None

    async def _run_task(
        self,
        record: BackgroundTaskRecord,
        cancel_ev: asyncio.Event,
    ) -> None:
        task_id = record.task_id
        max_steps = max(1, int(self.config.background_tasks.max_steps))
        timeout_sec = max(10, int(self.config.background_tasks.timeout_sec))
        usages: List[LLMUsage] = []
        trace_steps = []

        try:
            async with self._semaphore:
                if cancel_ev.is_set():
                    raise asyncio.CancelledError("Task cancelled before execution")

                started_rec = await self.backend.update_background_task(
                    task_id,
                    status="running",
                    progress_pct=10,
                    current_step="正在后台分析与调用工具…",
                    started_at=time.time(),
                )
                if started_rec is not None:
                    await self._emit_status(started_rec)

                sub_registry = self._build_sub_registry()
                profile_name = self._resolve_bg_profile_name()
                executor = ToolExecutor(self.llm_router)

                async def _on_status(
                    status: str,
                    tool_name: str,
                    label: str,
                    step: Optional[int],
                    limit: Optional[int],
                ) -> None:
                    step_val = step or 1
                    lim_val = max(1, limit or max_steps)
                    pct = min(90, 10 + int(75 * step_val / lim_val))
                    upd = await self.backend.update_background_task(
                        task_id,
                        status="running",
                        progress_pct=pct,
                        current_step=label or f"执行 {tool_name} ({step_val}/{lim_val})",
                    )
                    if upd is not None:
                        await self._emit_status(upd)

                async def _on_approval(
                    tool_name: str,
                    risk_level: RiskLevel,
                    summary: str,
                    details: Dict[str, Any],
                    wait_sec: int,
                ) -> tuple[bool, bool]:
                    if self.on_request_approval is None:
                        return False, False
                    wait_rec = await self.backend.update_background_task(
                        task_id,
                        status="waiting_approval",
                        current_step=f"等待审批：{summary[:60]}",
                    )
                    if wait_rec is not None:
                        await self._emit_status(wait_rec)
                    try:
                        return await self.on_request_approval(
                            task_id, tool_name, risk_level, summary, details, wait_sec
                        )
                    finally:
                        cur = await self.backend.get_background_task(task_id)
                        if cur is not None and cur.status == "waiting_approval":
                            resumed = await self.backend.update_background_task(
                                task_id,
                                status="running",
                                current_step=f"已收到审批响应，继续执行 {tool_name}…",
                            )
                            if resumed is not None:
                                await self._emit_status(resumed)

                async def _on_ask_user(
                    question: str,
                    options: List[str],
                    wait_sec: int,
                ) -> Optional[str]:
                    if self.on_ask_user is None:
                        return None
                    wait_rec = await self.backend.update_background_task(
                        task_id,
                        status="waiting_approval",
                        current_step=f"等待澄清：{question[:60]}",
                    )
                    if wait_rec is not None:
                        await self._emit_status(wait_rec)
                    try:
                        return await self.on_ask_user(task_id, question, options, wait_sec)
                    finally:
                        cur = await self.backend.get_background_task(task_id)
                        if cur is not None and cur.status == "waiting_approval":
                            resumed = await self.backend.update_background_task(
                                task_id,
                                status="running",
                                current_step="已收到用户答复，继续执行…",
                            )
                            if resumed is not None:
                                await self._emit_status(resumed)

                ctx = ToolExecutionContext(
                    turn_id=task_id,
                    session_id=record.source_session_id,
                    config=self.config,
                    llm_router=self.llm_router,
                    memory_backend=self.backend,
                    cancel_event=cancel_ev,
                    session_approved_tools=self._session_approved_tools,
                    on_status=_on_status,
                    on_approval=_on_approval,
                    on_ask_user=_on_ask_user,
                    usage_collector=usages,
                    task_runner=self,
                )

                messages: List[Dict[str, Any]] = [
                    {"role": "system", "content": _BG_TASK_SYSTEM_PROMPT},
                    {"role": "user", "content": record.goal_prompt},
                ]

                loop_res = await asyncio.wait_for(
                    executor.run(
                        messages=messages,
                        registry=sub_registry,
                        ctx=ctx,
                        profile_name=profile_name,
                        max_steps=max_steps,
                    ),
                    timeout=timeout_sec,
                )
                trace_steps = loop_res.trace_steps
                if not usages:
                    usages.extend(loop_res.usages)

                summary_text = (loop_res.final_text or "").strip() or "后台任务已完成。"
                artifact_path = self._write_report_artifact(record, summary_text)

                await self._record_bg_usage(record.source_session_id, task_id, usages)
                await self._archive_work_history(record, summary_text, trace_steps, status="completed")

                done_rec = await self.backend.update_background_task(
                    task_id,
                    status="completed",
                    progress_pct=100,
                    current_step="任务已完成",
                    result_summary=summary_text[:1000],
                    artifact_path=artifact_path,
                    finished_at=time.time(),
                )
                if done_rec is not None:
                    await self._emit_status(done_rec)

                await self._deliver_completion(record, summary_text, artifact_path)

        except asyncio.CancelledError:
            await self._record_bg_usage(record.source_session_id, task_id, usages)
            cur = await self.backend.get_background_task(task_id)
            if cur is not None and cur.status != "cancelled":
                cancelled_rec = await self.backend.update_background_task(
                    task_id,
                    status="cancelled",
                    current_step="任务已取消",
                    finished_at=time.time(),
                )
                if cancelled_rec is not None:
                    await self._emit_status(cancelled_rec)
            raise
        except asyncio.TimeoutError:
            err_msg = f"后台任务执行超时（超过 {timeout_sec} 秒）"
            await self._record_bg_usage(record.source_session_id, task_id, usages)
            await self._archive_work_history(record, err_msg, trace_steps, status="error")
            fail_rec = await self.backend.update_background_task(
                task_id,
                status="failed",
                current_step="执行超时",
                error_message=err_msg,
                finished_at=time.time(),
            )
            if fail_rec is not None:
                await self._emit_status(fail_rec)
        except Exception as exc:
            safe_err = self.config.redact_secrets(f"{type(exc).__name__}: {exc}")
            logger.exception("Background task failed task_id=%s", task_id)
            await self._record_bg_usage(record.source_session_id, task_id, usages)
            await self._archive_work_history(record, safe_err, trace_steps, status="error")
            fail_rec = await self.backend.update_background_task(
                task_id,
                status="failed",
                current_step="执行失败",
                error_message=safe_err[:500],
                finished_at=time.time(),
            )
            if fail_rec is not None:
                await self._emit_status(fail_rec)
        finally:
            self._tasks.pop(task_id, None)
            self._cancel_events.pop(task_id, None)

    def _write_report_artifact(self, record: BackgroundTaskRecord, summary_text: str) -> str:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.artifact_dir / f"{record.task_id}.md"
        content = (
            f"# {record.title}\n\n"
            f"- 任务 ID: `{record.task_id}`\n"
            f"- 原始指令: {record.goal_prompt}\n\n"
            f"---\n\n"
            f"{summary_text}\n"
        )
        report_path.write_text(content, encoding="utf-8")
        return str(report_path.resolve())

    async def _record_bg_usage(
        self,
        session_id: str,
        task_id: str,
        usages: List[LLMUsage],
    ) -> None:
        if not usages:
            return
        total_tokens = 0
        for u in usages:
            if (
                u.total_tokens <= 0
                and u.prompt_tokens <= 0
                and u.completion_tokens <= 0
                and u.estimated_cost_cny <= 0
            ):
                continue
            total_tokens += u.total_tokens or (u.prompt_tokens + u.completion_tokens)
            await self.backend.add_usage_record(
                UsageLedgerRecord(
                    session_id=session_id,
                    turn_id=task_id,
                    profile=u.profile,
                    provider=u.provider,
                    slot="bg_task",
                    model=u.model,
                    prompt_tokens=u.prompt_tokens,
                    prompt_cache_hit_tokens=u.prompt_cache_hit_tokens,
                    completion_tokens=u.completion_tokens,
                    estimated_cost_cny=u.estimated_cost_cny,
                )
            )
        alert_cfg = self.config.tools.usage_alert
        if not alert_cfg.enabled or self.on_soft_alert is None or total_tokens <= 0:
            return
        lt = time.localtime()
        day_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        summary = await self.backend.get_usage_summary(since_ts=day_start)
        reasons: List[str] = []
        if alert_cfg.turn_warn_tokens > 0 and total_tokens >= alert_cfg.turn_warn_tokens:
            reasons.append(f"后台任务消耗 {total_tokens} tokens（提醒阈值 {alert_cfg.turn_warn_tokens}）")
        if alert_cfg.daily_warn_cny > 0 and summary.total_cost_cny >= alert_cfg.daily_warn_cny:
            reasons.append(
                f"今日累计估算 ¥{summary.total_cost_cny:.4f}（提醒阈值 ¥{alert_cfg.daily_warn_cny:.2f}）"
            )
        if reasons:
            try:
                await self.on_soft_alert(f"后台任务用量软提醒：{'；'.join(reasons)}。")
            except Exception:
                pass

    async def _archive_work_history(
        self,
        record: BackgroundTaskRecord,
        summary_text: str,
        trace_steps: list,
        *,
        status: str,
    ) -> None:
        trace_payload = [
            {
                "step": s.step,
                "tool": s.tool_name,
                "args": s.arguments,
                "risk": s.risk_level,
                "status": s.status,
                "summary": s.summary,
                "elapsed_ms": s.elapsed_ms,
            }
            for s in trace_steps
        ]
        await self.backend.add_work_history(
            WorkHistoryRecord(
                work_id=new_work_id(),
                session_id=record.source_session_id,
                turn_id=record.task_id,
                goal=record.title[:200],
                result_summary=summary_text[:300],
                trace_json=json.dumps(trace_payload, ensure_ascii=False),
                step_count=len(trace_steps),
                status=status,
            )
        )

    async def _deliver_completion(
        self,
        record: BackgroundTaskRecord,
        summary_text: str,
        artifact_path: str,
    ) -> None:
        if self.on_notice is not None:
            notice = OutputProactiveNoticePayload(
                notice_id=new_notice_id(),
                task_id=record.task_id,
                kind="llm_summary",
                title=f"后台任务完成：{record.title}",
                content=summary_text[:300],
                created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            try:
                await self.on_notice(notice)
            except Exception:
                logger.debug("Failed sending proactive notice for %s", record.task_id)

        if self.on_artifact_capsule is not None and artifact_path:
            capsule = OutputArtifactCapsulePayload(
                turn_id=record.task_id,
                title=f"{record.title}.md",
                path=artifact_path,
                media_type="text",
                actions=["quick_look", "copy_file", "reveal"],
            )
            try:
                await self.on_artifact_capsule(capsule)
            except Exception:
                logger.debug("Failed sending artifact capsule for %s", record.task_id)
