from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from brain.config import AppConfig
from brain.llm_router import LLMRouter, LLMUsage, parse_dsml_tool_calls
from brain.memory.backend import MemoryBackend
from brain.rich_content import collect_media
from brain.memory.models import (
    BackgroundTaskRecord,
    BackgroundTaskStatus,
    UsageLedgerRecord,
    WorkHistoryRecord,
    new_bg_task_id,
    new_notice_id,
    new_session_id,
    new_work_id,
)
from brain.tools.base import ToolExecutionContext
from brain.tools.executor import ToolExecutor
from brain.tools.registry import ToolRegistry
from brain.watchdog import SemanticWatchdog, WatchdogDecision
from brain.worker import (
    WorkerMandate,
    artifact_ref,
    build_worker_registry,
    result_ref,
    verify_delivery,
)
from brain.worker_bundle import (
    collect_result_files,
    copy_task_results,
    write_report_artifact,
)
from brain.worker_runner import (
    WorkerRunMixin,
    clean_result_narration,
    result_summary_preview,
    with_doc_key_facts,
)
from protocol.models import (
    MediaItem,
    OutputArtifactCapsulePayload,
    OutputProactiveNoticePayload,
    RiskLevel,
    TaskFollowupAckedPayload,
    TaskStateChangedPayload,
    TaskStatusPayload,
    TaskWatchdogAlertPayload,
)

logger = logging.getLogger("qimingxing.task_runner")

# Worker resource-lock lease window. A conflicting writer keeps the task
# queued and retries at a human-scale cadence instead of failing instantly.
_TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "interrupted", "stalled"}
)

TaskStatusCallback = Callable[[TaskStatusPayload], Awaitable[None]]
NoticeCallback = Callable[[OutputProactiveNoticePayload], Awaitable[None]]
ArtifactCapsuleCallback = Callable[[OutputArtifactCapsulePayload], Awaitable[None]]
SoftAlertCallback = Callable[[str], Awaitable[None]]
TaskStateCallback = Callable[[TaskStateChangedPayload], Awaitable[None]]
TaskFollowupCallback = Callable[[TaskFollowupAckedPayload], Awaitable[None]]
WatchdogAlertCallback = Callable[[TaskWatchdogAlertPayload], Awaitable[None]]
ApprovalCallback = Callable[
    [str, str, RiskLevel, str, Dict[str, Any], int], Awaitable[tuple[bool, bool]]
]
AskUserCallback = Callable[[str, str, List[str], int], Awaitable[Optional[str]]]

def friendly_failure_fallback(raw_error: str) -> str:
    """Never show raw provider errors or stack traces to the user."""
    lower = (raw_error or "").lower()
    fallback = "任务执行中断，详情可在工作区查看。"
    if "timeout" in lower or "超时" in lower:
        fallback = "任务超时已停止，可以稍后重试。"
    elif "dsml" in lower or "tool_call" in lower:
        fallback = "模型接口这次没有正常返回，任务已停止，可以直接重试。"
    elif "approval" in lower or "审批" in lower:
        fallback = "等待授权没有完成，任务已停止。"
    elif "http" in lower or "api" in lower or "llm" in lower:
        fallback = "模型服务暂时不可用，任务已停止，稍后重试即可。"
    return fallback


class BackgroundTaskRunner(WorkerRunMixin):
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
        on_task_state_changed: Optional[TaskStateCallback] = None,
        on_followup_acked: Optional[TaskFollowupCallback] = None,
        on_watchdog_alert: Optional[WatchdogAlertCallback] = None,
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
        self.on_task_state_changed = on_task_state_changed
        self.on_followup_acked = on_followup_acked
        self.on_watchdog_alert = on_watchdog_alert

        max_conc = max(1, int(config.background_tasks.max_concurrency))
        self._semaphore = asyncio.Semaphore(max_conc)
        self._tasks: Dict[str, asyncio.Task[None]] = {}
        self._cancel_events: Dict[str, asyncio.Event] = {}
        self._resume_events: Dict[str, asyncio.Event] = {}
        self._watchdog_task: Optional[asyncio.Task[None]] = None
        self._watchdog_stop = asyncio.Event()
        self.watchdog = SemanticWatchdog(
            self.config.background_tasks,
            semantic_classifier=self._semantic_watchdog_verdict,
        )

    async def initialize(self) -> int:
        """Recover interrupted Workers according to auto_resume_on_restart."""
        auto = bool(self.config.background_tasks.auto_resume_on_restart)
        active = [
            t for t in await self.backend.list_background_tasks(limit=200)
            if t.status in (
                "queued", "planning", "running", "waiting_input",
                "waiting_approval", "verifying",
            )
        ]
        if not active:
            return 0
        if not auto:
            count = await self.backend.mark_unfinished_background_tasks_interrupted()
            logger.warning("Marked %d unfinished background tasks as interrupted (auto_resume=false)", count)
            return count

        resumed = 0
        recovered = 0
        for record in active:
            # Legacy tasks created before the Worker state machine have no
            # isolated session or checkpoint. Never auto-replay them.
            if not record.worker_session_id and not record.checkpoint:
                await self.backend.update_background_task(
                    record.task_id,
                    status="interrupted",
                    current_step="旧版任务无检查点，等待手动重试",
                )
                recovered += 1
                continue
            checkpoint = record.checkpoint if isinstance(record.checkpoint, dict) else {}
            if record.status in ("waiting_approval", "waiting_input"):
                # An approval or clarification request cannot survive a process
                # restart; keep the task parked for an explicit user decision.
                await self.backend.update_background_task(
                    record.task_id,
                    status=record.status,
                    current_step="重启后等待主人确认，未自动重放",
                    last_heartbeat_at=time.time(),
                )
                recovered += 1
                continue
            if checkpoint.get("has_side_effect"):
                await self.backend.update_background_task(
                    record.task_id,
                    status="waiting_approval",
                    current_step="上次中断发生在写操作，是否继续重试？",
                )
                recovered += 1
                continue
            await self.backend.update_background_task(
                record.task_id,
                status="queued",
                current_step="检测到未完成任务，正在从检查点恢复…",
            )
            cancel_ev = asyncio.Event()
            self._cancel_events[record.task_id] = cancel_ev
            self._tasks[record.task_id] = asyncio.create_task(
                self._run_task(record, cancel_ev)
            )
            resumed += 1
            recovered += 1
        logger.info("Recovered %d background tasks (auto_resume=true)", resumed)
        return recovered

    async def shutdown(self) -> None:
        """Cancel all in-flight background tasks cleanly on process shutdown."""
        await self.stop_watchdog()
        for ev in self._cancel_events.values():
            ev.set()
        pending = [t for t in self._tasks.values() if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._cancel_events.clear()
        self._resume_events.clear()

    async def start_watchdog(self) -> None:
        if not self.config.background_tasks.watchdog_enabled:
            return
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return
        self._watchdog_stop.clear()
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        logger.info(
            "SemanticWatchdog started (interval=%.1fs)",
            self.config.background_tasks.watchdog_interval_sec,
        )

    async def stop_watchdog(self) -> None:
        self._watchdog_stop.set()
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None

    async def _watchdog_loop(self) -> None:
        interval = max(5.0, float(self.config.background_tasks.watchdog_interval_sec))
        while not self._watchdog_stop.is_set():
            try:
                await self.watchdog_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Watchdog tick degraded: %s", exc)
            try:
                await asyncio.wait_for(self._watchdog_stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def watchdog_tick(self, *, now: Optional[float] = None) -> List[WatchdogDecision]:
        """Run one zero-token rule screening + semantic verdict pass."""
        decisions: List[WatchdogDecision] = []
        active = await self.backend.list_background_tasks(limit=200)
        for record in active:
            if record.status not in ("planning", "running", "verifying"):
                continue
            decision = await self.watchdog.evaluate(record, now=now)
            decisions.append(decision)
            if decision.action == "continue":
                continue
            await self._apply_watchdog_decision(record, decision)
        return decisions

    async def _apply_watchdog_decision(
        self,
        record: BackgroundTaskRecord,
        decision: WatchdogDecision,
    ) -> None:
        task_id = record.task_id
        state = dict(record.watchdog_state or {})
        state["last_check_at"] = time.time()
        state["last_action"] = decision.action
        state["last_reason"] = decision.reason
        if decision.cooldown_sec:
            state["cooldown_until"] = time.time() + decision.cooldown_sec

        if decision.action == "nudge":
            state["nudge_count"] = int(state.get("nudge_count") or 0) + 1
            await self.backend.add_task_inbox(
                task_id,
                sender="watchdog",
                content=(
                    "【Watchdog 纠偏】" + (decision.reason or "当前路径没有进展。")
                    + " 请立即换一种方法；若被环境或权限卡住，请直接说明阻塞点，不要重复同一调用。"
                ),
            )
            await self.backend.update_background_task(
                task_id,
                status="running",
                current_step="Watchdog 已注入纠偏提示",
                watchdog_state=state,
                last_heartbeat_at=time.time(),
            )
        elif decision.action == "escalate":
            state["escalated"] = True
            state["block_reason"] = decision.reason
            # Pause at the next step boundary until the user sends a followup.
            resume = self._resume_events.get(task_id)
            if resume is not None:
                resume.clear()
            await self.backend.update_background_task(
                task_id,
                status="waiting_input",
                current_step="Watchdog：等待主人补充环境或决策",
                watchdog_state=state,
                last_heartbeat_at=time.time(),
            )
        elif decision.action == "abort":
            state["aborted"] = True
            state["block_reason"] = decision.reason
            await self.backend.update_background_task(
                task_id,
                status="failed",
                current_step="Watchdog 已中止无解循环",
                result_summary=(decision.reason or "任务已停止")[:120],
                error_message=(decision.reason or "Watchdog 判定任务无解")[:500],
                watchdog_state=state,
                last_heartbeat_at=time.time(),
                finished_at=time.time(),
            )
            cancel_ev = self._cancel_events.get(task_id)
            if cancel_ev is not None:
                cancel_ev.set()
            task = self._tasks.get(task_id)
            if task is not None and not task.done():
                task.cancel()

        updated = await self.backend.get_background_task(task_id)
        if updated is not None:
            await self._emit_status(updated)
        await self._emit_watchdog_alert(task_id, decision)

    async def _emit_watchdog_alert(
        self, task_id: str, decision: WatchdogDecision
    ) -> None:
        if self.on_watchdog_alert is None:
            return
        try:
            await self.on_watchdog_alert(
                TaskWatchdogAlertPayload(
                    task_id=task_id,
                    action=decision.action,
                    reason=decision.reason,
                )
            )
        except Exception:
            logger.debug("Failed emitting watchdog alert for %s", task_id)

    async def _semantic_watchdog_verdict(self, signals: Dict[str, Any]) -> str:
        """Ask the fast utility slot only after a rule hit.

        Mock/offline routing is skipped so unit tests and local no-key setups
        never make surprise network calls.
        """
        try:
            cur = self.llm_router.get_profile()
            if cur.provider == "mock":
                return ""
        except Exception:
            return ""
        if not self.config.has_explicit_routing:
            return ""
        prompt = (
            "你是启明星后台 Worker 的语义看门狗。根据目标与最近信号判断 Worker 是否仍值得继续。\n"
            '只输出 JSON：{"action":"continue|nudge|escalate|abort","reason":"一句话中文理由"}。\n'
            "continue=复杂但合理；nudge=局部死胡同可换思路；escalate=需要用户决策或环境缺失；"
            "abort=确认无解死循环。\n"
            f"目标: {str(signals.get('goal') or '')[:500]}\n"
            f"信号: {json.dumps(signals, ensure_ascii=False)[:1800]}"
        )
        try:
            chunks: List[str] = []
            async for token in self.llm_router.stream_chat(
                [{"role": "user", "content": prompt}],
                profile_name="@slot:fast_utility",
                json_mode=True,
            ):
                chunks.append(token)
                if sum(len(c) for c in chunks) > 1200:
                    break
            return "".join(chunks)
        except Exception as exc:
            logger.debug("Watchdog semantic call failed: %s", exc)
            return ""

    def active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if not t.done())

    async def submit(
        self,
        *,
        title: str,
        goal_prompt: str,
        source_session_id: str = "",
        packs: Optional[List[str]] = None,
        mandate: Optional[Dict[str, Any]] = None,
        origin: str = "explicit_user",
    ) -> BackgroundTaskRecord:
        task_id = new_bg_task_id()
        now = time.time()
        clean_title = (title or "后台分析任务").strip()[:120]
        raw_mandate = dict(mandate or {})
        if packs is not None:
            raw_mandate["allowed_packs"] = list(packs)
        raw_mandate.setdefault("origin", origin)
        mandate_obj = WorkerMandate.from_dict(raw_mandate)
        record = BackgroundTaskRecord(
            task_id=task_id,
            title=clean_title,
            goal_prompt=goal_prompt.strip(),
            source_session_id=source_session_id,
            status="queued",
            progress_pct=0,
            current_step="已加入后台队列，等待执行…",
            slot_used="worker" if mandate_obj.allowed_packs else "bg_task",
            worker_session_id=new_session_id(),
            origin=origin,
            mandate=mandate_obj.to_dict(),
            checkpoint={},
            result_refs=[],
            watchdog_state={},
            last_heartbeat_at=now,
            created_at=now,
        )
        await self.backend.add_background_task(record)
        cancel_ev = asyncio.Event()
        self._cancel_events[task_id] = cancel_ev
        bg_task = asyncio.create_task(self._run_task(record, cancel_ev))
        self._tasks[task_id] = bg_task
        await self._emit_status(record)
        return record

    async def followup(self, task_id: str, instruction: str) -> tuple[bool, str]:
        rec = await self.backend.get_background_task(task_id)
        if rec is None:
            return False, f"未找到后台任务 `{task_id}`。"
        if rec.status in ("completed", "failed", "cancelled", "interrupted", "stalled"):
            return False, f"后台任务 `{task_id}` 当前状态为 {rec.status}，无法接收新指令。"
        clean = (instruction or "").strip()
        if not clean:
            return False, "补充指令不能为空。"
        await self.backend.add_task_inbox(task_id, sender="user_followup", content=clean)
        resume = self._resume_events.get(task_id)
        if resume is not None:
            resume.set()
        if rec.status in ("waiting_input", "queued", "waiting_approval"):
            state = dict(rec.watchdog_state or {})
            state["escalated"] = False
            state["block_reason"] = ""
            state["cooldown_until"] = time.time() + 30
            await self.backend.update_background_task(
                task_id,
                status="running",
                current_step="已收到补充指令，继续执行…",
                watchdog_state=state,
            )
        message = f"已把补充要求转告给任务 `{task_id}`。"
        if self.on_followup_acked is not None:
            try:
                await self.on_followup_acked(
                    TaskFollowupAckedPayload(
                        task_id=task_id, accepted=True, message=message
                    )
                )
            except Exception:
                logger.debug("Failed emitting followup ack for %s", task_id)
        return True, message

    async def cancel(self, task_id: str) -> tuple[bool, str]:
        rec = await self.backend.get_background_task(task_id)
        if rec is None:
            return False, f"未找到后台任务 `{task_id}`。"
        if rec.status in ("completed", "failed", "cancelled", "interrupted"):
            return False, f"后台任务 `{task_id}` 当前状态为 {rec.status}，无需取消。"

        ev = self._cancel_events.get(task_id)
        if ev is not None:
            ev.set()
        resume = self._resume_events.get(task_id)
        if resume is not None:
            resume.set()
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
        # Parsing Office previews is expensive; only build attachment media on
        # terminal states where the shell actually renders them.
        result_media = (
            [MediaItem(**item) for item in collect_media(rec.result_files)]
            if rec.status in _TERMINAL_TASK_STATUSES
            else []
        )
        if self.on_task_status is not None:
            payload = TaskStatusPayload(
                task_id=rec.task_id,
                title=rec.title,
                status=rec.status,
                progress_pct=rec.progress_pct,
                current_step=rec.current_step,
                active_count=self.active_count(),
                result_summary=rec.result_summary,
                artifact_path=rec.artifact_path,
                bundle_path=rec.bundle_path,
                result_files=list(rec.result_files),
                result_media=result_media,
                error_message=rec.error_message,
            )
            try:
                await self.on_task_status(payload)
            except Exception:
                logger.debug("Failed emitting task.status for %s", rec.task_id)
        if self.on_task_state_changed is not None:
            state = rec.watchdog_state if isinstance(rec.watchdog_state, dict) else {}
            try:
                await self.on_task_state_changed(
                    TaskStateChangedPayload(
                        task_id=rec.task_id,
                        title=rec.title,
                        status=rec.status,
                        current_step=rec.current_step,
                        progress_pct=rec.progress_pct,
                        block_reason=str(
                            state.get("block_reason")
                            or (
                                rec.current_step
                                if rec.status in ("waiting_approval", "waiting_input")
                                else ""
                            )
                            or rec.error_message
                            or ""
                        ),
                        result_refs=list(rec.result_refs),
                        bundle_path=rec.bundle_path,
                        result_files=list(rec.result_files),
                        result_media=result_media,
                    )
                )
            except Exception:
                logger.debug("Failed emitting task.state_changed for %s", rec.task_id)

    def _build_sub_registry(self, record: Optional[BackgroundTaskRecord] = None) -> ToolRegistry:
        mandate = WorkerMandate.from_dict(record.mandate if record is not None else {})
        if not mandate.allowed_packs:
            # Legacy/low-level submits keep the pre-orchestration full worker
            # surface; Router dispatch always supplies explicit packs.
            sub = ToolRegistry()
            for tool in self.tool_registry.list_tools():
                if tool.name in ("bg_task_submit", "task_dispatch"):
                    continue
                sub.register(tool)
            return sub
        return build_worker_registry(self.tool_registry, mandate.allowed_packs)

    def _resolve_bg_profile_name(self) -> Optional[str]:
        try:
            cur = self.llm_router.get_profile()
            if cur.provider == "mock":
                return None
        except Exception:
            pass
        if self.config.has_explicit_routing:
            return "@slot:worker"
        return None

    async def _humanize_failure(
        self, record: BackgroundTaskRecord, raw_error: str
    ) -> str:
        """One short, user-facing reason. Raw errors stay in error_message."""
        lower = (raw_error or "").lower()
        fallback = friendly_failure_fallback(raw_error)
        try:
            cur = self.llm_router.get_profile()
            if cur.provider == "mock" or not self.config.has_explicit_routing:
                return fallback
            prompt = (
                "用一句中文告诉用户后台任务为什么没完成，最多 40 字；"
                "不要出现错误码、堆栈、文件路径或英文技术名词。\n"
                f"任务：{record.title}\n原始错误：{str(raw_error)[:800]}"
            )

            async def _ask() -> str:
                chunks: List[str] = []
                async for token in self.llm_router.stream_chat(
                    [{"role": "user", "content": prompt}],
                    profile_name="@slot:fast_utility",
                ):
                    chunks.append(token)
                    if sum(len(c) for c in chunks) > 160:
                        break
                return " ".join("".join(chunks).split())

            text = await asyncio.wait_for(_ask(), timeout=6.0)
            if text:
                return text[:80]
        except Exception:
            logger.debug("Failure humanization skipped", exc_info=True)
        return fallback

    @staticmethod
    def _collect_result_files(trace_steps: list) -> List[str]:
        """Backward-compatible alias; implementation lives in brain.worker_bundle."""
        return collect_result_files(trace_steps)

    async def _record_bg_usage(
        self,
        session_id: str,
        task_id: str,
        usages: List[LLMUsage],
        *,
        slot: str = "bg_task",
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
                    slot=slot,
                    model=u.model,
                    prompt_tokens=u.prompt_tokens,
                    prompt_cache_hit_tokens=u.prompt_cache_hit_tokens,
                    cached_input_tokens=u.cached_input_tokens or u.prompt_cache_hit_tokens,
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
