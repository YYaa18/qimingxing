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
_LOCK_WAIT_SECONDS = 120.0
_LOCK_RETRY_SECONDS = 3.0
_LOCK_LEASE_SECONDS = 120.0

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

_BG_TASK_SYSTEM_PROMPT = (
    "你是启明星（Qimingxing）后台异步长任务执行引擎。\n"
    "当前运行在独立隔离上下文中，主对话不受阻塞。\n"
    "请严谨调用可用工具收集与核实信息，禁止编造未观测的结果或路径；\n"
    "如果遇到需要用户确认的操作，请正常发起工具调用以触发原生审批弹窗。\n"
    "最终输出必须结果优先：先用 1-3 句给出结论与关键数字，再说明未完成/异常。"
    "产物文件由系统以附件卡片自动呈现，正文不要罗列绝对路径或文件清单；"
    "只生成用户要求的最终交付物，不要在输出目录制造校验/中间/临时文件；"
    "办公文件的表格键值匹配重命名（如按序号/票号改名 PDF）优先用 doc_match_rename 一步完成，"
    "它已产出副本与核对报告，不要拆成 sheet_transform + doc_rename 多步；"
    "禁止复述工具调用步骤、内心独白、‘我将/我会先…’等过程性叙述。"
)

_RESULT_HEADING_RE = re.compile(
    r"^\s*(?:#{1,4}\s*)?(?:结论|结果|交付结果|核对结果|最终结果|交付物)\s*[:：]?\s*$",
    re.MULTILINE,
)
_PROCESS_LEAD_RE = re.compile(
    r"^\s*(?:"
    r"我(?:先|会|将|已经|已|来|再)|"
    r"首先|接下来|现在|第一步|让我|下面我|先(?:列出|读取|查看|检查|扫描|枚举)"
    r")[^\n]{0,160}[。\n]",
)


def clean_result_narration(text: str) -> str:
    """Drop a process preface before the real result section.

    Workers occasionally open with "我先列出目录…" before the actual
    conclusion. Prefer an explicit result heading; otherwise strip one
    leading process sentence so summaries start with the deliverable.
    """
    cleaned = str(text or "").strip()
    if not cleaned:
        return cleaned
    cleaned, dsml_calls = parse_dsml_tool_calls(cleaned)
    if dsml_calls and not cleaned:
        cleaned = "任务已完成，但上游返回了无法解析的工具调用；建议重试。"
    elif "<｜｜DSML" in cleaned:
        cleaned = re.sub(r"<[^>]*DSML[^>]*>.*?</[^>]*DSML[^>]*>", "", cleaned, flags=re.DOTALL)
        cleaned = re.sub(r"<[^>]*DSML[^>]*>", "", cleaned).strip()
        if not cleaned:
            cleaned = "任务已完成，但上游返回了无法解析的工具调用；建议重试。"
    cleaned = re.sub(r"^\*{1,3}\s+", "", cleaned)
    match = _RESULT_HEADING_RE.search(cleaned, 0, min(len(cleaned), 400))
    if match and match.start() > 0:
        cleaned = cleaned[match.start():].lstrip()
        cleaned = re.sub(r"^#{1,4}\s*", "", cleaned, count=1).lstrip()
        heading_end = cleaned.find("\n")
        if heading_end >= 0:
            cleaned = cleaned[heading_end + 1:].lstrip()
    for _ in range(2):
        stripped = _PROCESS_LEAD_RE.sub("", cleaned, count=1)
        if stripped == cleaned:
            break
        stripped = stripped.lstrip()
        if not stripped:
            break
        cleaned = stripped
    return cleaned.strip()


def result_summary_preview(text: str, limit: int = 500) -> str:
    """Truncate for the live card at a sentence boundary, not mid-word."""
    cleaned = str(text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    head = cleaned[: max(1, limit - 2)]
    cut = max(
        head.rfind("\n"),
        head.rfind("。"),
        head.rfind("；"),
        head.rfind("！"),
        head.rfind("？"),
    )
    if cut >= int(limit * 0.55):
        return (head[: cut + 1].rstrip() + "\n…")[:limit]
    return (head.rstrip() + "…")[:limit]


def with_doc_key_facts(summary_text: str, trace_steps: list) -> str:
    """Guarantee the invoice workflow leads with its deterministic numbers."""
    match_fact = next(
        (
            step.summary
            for step in reversed(trace_steps)
            if getattr(step, "tool_name", "") == "doc_match_rename"
            and getattr(step, "status", "") == "ok"
            and getattr(step, "summary", "")
        ),
        "",
    )
    if match_fact and "关键数字" not in summary_text[:80]:
        return f"关键数字：{match_fact}\n\n{summary_text}"
    return summary_text


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

    async def _run_task(
        self,
        record: BackgroundTaskRecord,
        cancel_ev: asyncio.Event,
    ) -> None:
        task_id = record.task_id
        mandate = WorkerMandate.from_dict(record.mandate)
        max_steps = max(
            1, min(int(self.config.background_tasks.max_steps), mandate.max_steps)
        )
        timeout_sec = max(
            10,
            min(int(self.config.background_tasks.timeout_sec), mandate.max_wall_seconds),
        )
        usages: List[LLMUsage] = []
        trace_steps = []
        held_locks: List[str] = []
        usage_slot = "worker" if mandate.allowed_packs else "bg_task"
        self._resume_events.setdefault(task_id, asyncio.Event()).set()

        try:
            async with self._semaphore:
                if cancel_ev.is_set():
                    raise asyncio.CancelledError("Task cancelled before execution")

                lock_keys = mandate.resource_keys()
                for key in lock_keys:
                    acquired = False
                    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
                    waited = 0.0
                    while not acquired:
                        acquired = await self.backend.acquire_resource_lock(
                            key, task_id, mode="exclusive",
                            lease_seconds=_LOCK_LEASE_SECONDS,
                        )
                        if acquired:
                            break
                        if cancel_ev.is_set():
                            raise asyncio.CancelledError("Task cancelled while waiting for lock")
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        waited += min(_LOCK_RETRY_SECONDS, remaining)
                        await self.backend.update_background_task(
                            task_id,
                            status="queued",
                            current_step=f"等待资源锁 {key}（已等待 {int(waited)} 秒）…",
                        )
                        await asyncio.sleep(min(_LOCK_RETRY_SECONDS, remaining))
                    if not acquired:
                        raise RuntimeError(
                            f"等待资源锁超时（{int(_LOCK_WAIT_SECONDS)} 秒）: {key}"
                        )
                    held_locks.append(key)

                started_rec = await self.backend.update_background_task(
                    task_id,
                    status="running",
                    progress_pct=10,
                    current_step="正在后台分析与调用工具…",
                    started_at=time.time(),
                )
                if started_rec is not None:
                    await self._emit_status(started_rec)

                sub_registry = self._build_sub_registry(record)
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
                        status="waiting_input",
                        current_step=f"等待澄清：{question[:60]}",
                    )
                    if wait_rec is not None:
                        await self._emit_status(wait_rec)
                    try:
                        return await self.on_ask_user(task_id, question, options, wait_sec)
                    finally:
                        cur = await self.backend.get_background_task(task_id)
                        if cur is not None and cur.status == "waiting_input":
                            resumed = await self.backend.update_background_task(
                                task_id,
                                status="running",
                                current_step="已收到用户答复，继续执行…",
                            )
                            if resumed is not None:
                                await self._emit_status(resumed)

                # "Remember for this session" is scoped to this one background
                # task; it must never auto-approve a CONFIRM tool in another task.
                task_approved_tools: set[str] = set()
                async def _on_step_start(
                    step_idx: int, working_messages: List[Dict[str, Any]]
                ) -> None:
                    # If watchdog escalated, pause at a clean step boundary
                    # until the user sends a followup. Task wall-clock timeout
                    # still bounds the wait.
                    resume = self._resume_events.get(task_id)
                    if resume is not None and not resume.is_set():
                        cur = await self.backend.get_background_task(task_id)
                        if cur is not None and cur.status == "waiting_input":
                            await resume.wait()
                    inbox = await self.backend.consume_task_inbox(task_id)
                    for item in inbox:
                        working_messages.append(
                            {
                                "role": "system",
                                "content": f"[运行中补充指令/{item.sender}] {item.content}",
                            }
                        )
                    for key in held_locks:
                        await self.backend.renew_resource_lock(key, task_id, lease_seconds=120.0)
                    latest = await self.backend.get_background_task(task_id)
                    base_cp = latest.checkpoint if latest is not None else record.checkpoint
                    now = time.time()
                    next_cp = {
                        **base_cp,
                        "step": step_idx,
                        "prompt_tokens_total": sum(u.prompt_tokens for u in usages),
                        "last_heartbeat_at": now,
                    }
                    state = dict(
                        latest.watchdog_state
                        if latest is not None and isinstance(latest.watchdog_state, dict)
                        else record.watchdog_state or {}
                    )
                    # A live LLM/tool call is an intentional long activity. The
                    # rule layer must not treat its quiet period as a stalled
                    # Worker; extra step callbacks clear this marker.
                    state["active_phase"] = "step"
                    state["active_since"] = now
                    record.checkpoint = next_cp
                    record.watchdog_state = state
                    await self.backend.update_background_task(
                        task_id,
                        status="running",
                        last_heartbeat_at=now,
                        checkpoint=next_cp,
                        watchdog_state=state,
                    )

                async def _on_step_complete(step_idx: int, step: Any) -> None:
                    now = time.time()
                    latest = await self.backend.get_background_task(task_id)
                    state = dict(
                        latest.watchdog_state
                        if latest is not None and isinstance(latest.watchdog_state, dict)
                        else record.watchdog_state or {}
                    )
                    base_cp = (
                        latest.checkpoint
                        if latest is not None and isinstance(latest.checkpoint, dict)
                        else record.checkpoint
                    )
                    signature = hashlib.sha1(
                        json.dumps(
                            [step.tool_name, step.arguments],
                            ensure_ascii=False,
                            sort_keys=True,
                        ).encode("utf-8")
                    ).hexdigest()
                    if state.get("last_signature") == signature:
                        state["repeat_count"] = int(state.get("repeat_count") or 0) + 1
                    else:
                        state["repeat_count"] = 1
                        state["last_signature"] = signature
                    if step.status == "ok":
                        state["error_streak"] = 0
                        state["last_progress_at"] = now
                        state["last_progress_step"] = step_idx
                    else:
                        state["error_streak"] = int(state.get("error_streak") or 0) + 1
                    state["last_tool"] = step.tool_name
                    state["last_status"] = step.status
                    state.pop("active_phase", None)
                    state.pop("active_since", None)
                    record.watchdog_state = state

                    cp = {
                        **base_cp,
                        "step": step_idx,
                        "last_tool": step.tool_name,
                        "last_status": step.status,
                        "has_side_effect": bool(
                            base_cp.get("has_side_effect")
                            or step.tool_name in (
                                "shell_exec",
                                "macos_notes_create",
                                "macos_calendar_create",
                                "macos_reminders_create",
                                "macos_mail_draft",
                                "sheet_transform",
                                "pdf_ops",
                                "doc_generate",
                            )
                            or (
                                step.tool_name == "browser_act"
                                and str((step.arguments or {}).get("action") or "")
                                in ("click", "fill", "select", "press")
                            )
                        ),
                        "prompt_tokens_total": sum(u.prompt_tokens for u in usages),
                        "last_progress_at": state.get("last_progress_at"),
                        "last_heartbeat_at": now,
                    }
                    record.checkpoint = cp
                    for key in held_locks:
                        await self.backend.renew_resource_lock(key, task_id, lease_seconds=120.0)
                    await self.backend.update_background_task(
                        task_id,
                        status="running",
                        last_heartbeat_at=now,
                        checkpoint=cp,
                        watchdog_state=state,
                    )

                ctx = ToolExecutionContext(
                    turn_id=task_id,
                    session_id=record.worker_session_id or record.source_session_id,
                    config=self.config,
                    llm_router=self.llm_router,
                    memory_backend=self.backend,
                    cancel_event=cancel_ev,
                    session_approved_tools=task_approved_tools,
                    on_status=_on_status,
                    on_approval=_on_approval,
                    on_ask_user=_on_ask_user,
                    usage_collector=usages,
                    task_runner=self,
                    allowed_tools={tool.name for tool in sub_registry.list_tools()},
                    worker_mandate=mandate,
                    on_step_start=_on_step_start,
                    on_step_complete=_on_step_complete,
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

                # Deterministic pre-delivery verification. One bounded rework
                # pass is allowed; we never silently claim a missing file.
                verify_ok, verify_issues = verify_delivery(
                    summary=summary_text, artifact_paths=[], mandate=mandate
                )
                if not verify_ok:
                    await self.backend.update_background_task(
                        task_id,
                        status="verifying",
                        current_step="交付自检未通过，正在局部返工…",
                    )
                    rework_messages = list(messages) + [
                        {
                            "role": "user",
                            "content": (
                                "交付自检发现以下问题，请仅修正它们并重新给出最终结论，"
                                "不要编造或声称未真实生成的文件；上一版草稿中的过程叙述不要重复：\n- "
                                + "\n- ".join(verify_issues[:5])
                            ),
                        },
                    ]
                    rework = await asyncio.wait_for(
                        executor.run(
                            messages=rework_messages,
                            registry=sub_registry,
                            ctx=ctx,
                            profile_name=profile_name,
                            max_steps=min(3, max_steps),
                        ),
                        timeout=min(120.0, float(timeout_sec)),
                    )
                    trace_steps.extend(rework.trace_steps)
                    usages.extend(rework.usages)
                    summary_text = (rework.final_text or "").strip() or summary_text
                    verify_ok, verify_issues = verify_delivery(
                        summary=summary_text, artifact_paths=[], mandate=mandate
                    )
                    if not verify_ok:
                        summary_text = (
                            "⚠️ 交付自检未通过："
                            + "；".join(verify_issues[:3])
                            + "\n\n"
                            + summary_text
                        )

                summary_text = clean_result_narration(summary_text)
                summary_text = with_doc_key_facts(summary_text, trace_steps)
                artifact_path = write_report_artifact(
                    self.artifact_dir, record, summary_text
                )
                bundle_path = str(Path(artifact_path).parent)
                result_files = [artifact_path] + copy_task_results(
                    Path(bundle_path), collect_result_files(trace_steps)
                )
                refs = [result_ref(task_id, "report"), artifact_ref(task_id, artifact_path)]
                seen_steps = set()
                for trace_step in trace_steps[-10:]:
                    step_no = int(getattr(trace_step, "step", 0) or 0)
                    if step_no <= 0 or step_no in seen_steps:
                        continue
                    seen_steps.add(step_no)
                    refs.append(result_ref(task_id, f"step_{step_no}"))

                await self._record_bg_usage(record.source_session_id, task_id, usages, slot=usage_slot)
                await self._archive_work_history(record, summary_text, trace_steps, status="completed")

                done_rec = await self.backend.update_background_task(
                    task_id,
                    status="completed",
                    progress_pct=100,
                    current_step="任务已完成",
                    result_summary=result_summary_preview(summary_text),
                    artifact_path=artifact_path,
                    bundle_path=bundle_path,
                    result_files=result_files,
                    result_refs=refs,
                    last_heartbeat_at=time.time(),
                    finished_at=time.time(),
                )
                if done_rec is not None:
                    await self._emit_status(done_rec)

                await self._deliver_completion(record, summary_text, artifact_path)

        except asyncio.CancelledError:
            await self._record_bg_usage(record.source_session_id, task_id, usages, slot=usage_slot)
            cur = await self.backend.get_background_task(task_id)
            if cur is not None and cur.status not in ("cancelled", "failed", "stalled"):
                cancelled_rec = await self.backend.update_background_task(
                    task_id,
                    status="cancelled",
                    current_step="任务已取消",
                    finished_at=time.time(),
                )
                if cancelled_rec is not None:
                    await self._emit_status(cancelled_rec)
            elif cur is not None and cur.status == "failed" and not cur.artifact_path:
                # Watchdog abort: preserve the scene and partial trace so the
                # owner can inspect why the Worker was stopped.
                lines = [
                    "任务被 Watchdog 中止，已保存中止前的执行现场。",
                    f"原因: {cur.error_message or '无解循环'}",
                    "",
                    "最近步骤:",
                ]
                for step in trace_steps[-8:]:
                    lines.append(
                        f"- step {step.step}: {step.tool_name} -> {step.status}: {step.summary[:120]}"
                    )
                partial = "\n".join(lines)
                path = write_report_artifact(self.artifact_dir, record, partial)
                refs = [result_ref(task_id, "report"), artifact_ref(task_id, path)]
                await self._archive_work_history(record, partial, trace_steps, status="error")
                updated = await self.backend.update_background_task(
                    task_id,
                    artifact_path=path,
                    result_refs=refs,
                    current_step="Watchdog 已中止并保存现场",
                    last_heartbeat_at=time.time(),
                )
                if updated is not None:
                    await self._emit_status(updated)
            raise
        except asyncio.TimeoutError:
            err_msg = f"后台任务执行超时（超过 {timeout_sec} 秒）"
            await self._record_bg_usage(record.source_session_id, task_id, usages, slot=usage_slot)
            await self._archive_work_history(record, err_msg, trace_steps, status="error")
            friendly = await self._humanize_failure(record, err_msg)
            fail_rec = await self.backend.update_background_task(
                task_id,
                status="failed",
                current_step="执行超时",
                result_summary=friendly,
                error_message=err_msg,
                finished_at=time.time(),
            )
            if fail_rec is not None:
                await self._emit_status(fail_rec)
        except Exception as exc:
            safe_err = self.config.redact_secrets(f"{type(exc).__name__}: {exc}")
            logger.exception("Background task failed task_id=%s", task_id)
            await self._record_bg_usage(record.source_session_id, task_id, usages, slot=usage_slot)
            await self._archive_work_history(record, safe_err, trace_steps, status="error")
            friendly = await self._humanize_failure(record, safe_err)
            fail_rec = await self.backend.update_background_task(
                task_id,
                status="failed",
                current_step="执行失败",
                result_summary=friendly,
                error_message=safe_err[:500],
                finished_at=time.time(),
            )
            if fail_rec is not None:
                await self._emit_status(fail_rec)
        finally:
            try:
                from brain.browser_runtime import shutdown_runtime

                await shutdown_runtime(task_id)
            except Exception:
                logger.debug("Failed closing browser runtime for %s", task_id, exc_info=True)
            for key in held_locks:
                await self.backend.release_resource_lock(key, task_id)
            self._tasks.pop(task_id, None)
            self._cancel_events.pop(task_id, None)
            self._resume_events.pop(task_id, None)

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
