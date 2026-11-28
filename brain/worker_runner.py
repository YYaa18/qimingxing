from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from protocol.models import (
    OutputArtifactCapsulePayload,
    OutputProactiveNoticePayload,
    RiskLevel,
)

from brain.browser_runtime import shutdown_runtime
from brain.llm_router import LLMUsage, parse_dsml_tool_calls
from brain.memory.models import BackgroundTaskRecord
from brain.tools.base import ToolExecutionContext
from brain.tools.executor import ToolExecutor
from brain.worker import WorkerMandate, artifact_ref, result_ref, verify_delivery
from brain.worker_bundle import (
    collect_result_files,
    copy_task_results,
    write_report_artifact,
)

logger = logging.getLogger("qimingxing.worker.runner")

_LOCK_WAIT_SECONDS = 120.0
_LOCK_RETRY_SECONDS = 3.0
_LOCK_LEASE_SECONDS = 120.0


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


class WorkerRunMixin:
    """Background task execution loop (moved verbatim from BackgroundTaskRunner)."""

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

