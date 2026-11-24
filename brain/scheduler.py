from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Awaitable, Callable, List, Optional

from brain.config import AppConfig
from brain.llm_router import LLMRouter
from brain.memory.backend import MemoryBackend
from brain.memory.models import (
    ScheduleKind,
    ScheduledTaskRecord,
    UsageLedgerRecord,
    new_notice_id,
)
from protocol.models import OutputProactiveNoticePayload

logger = logging.getLogger("qimingxing.scheduler")

_REL_OFFSET_RE = re.compile(r"^\+?(\d+(?:\.\d+)?)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|day|days)?$", re.IGNORECASE)
_DAILY_HM_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_EMOTION_TAG_RE = re.compile(r"^\[EMOTION:[a-zA-Z]+\]\s*")

NoticeCallback = Callable[[OutputProactiveNoticePayload], Awaitable[None]]
SoftAlertCallback = Callable[[str], Awaitable[None]]


def _parse_duration_seconds(expr: str) -> Optional[float]:
    m = _REL_OFFSET_RE.match((expr or "").strip())
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "s").lower()
    if unit.startswith("m"):
        return val * 60.0
    if unit.startswith("h"):
        return val * 3600.0
    if unit.startswith("d"):
        return val * 86400.0
    return val


def compute_next_run_at(
    schedule_kind: ScheduleKind,
    schedule_expr: str,
    *,
    now: Optional[float] = None,
) -> Optional[float]:
    """Compute next Unix timestamp for once / daily / interval schedule expressions."""
    current = now if now is not None else time.time()
    expr = (schedule_expr or "").strip()

    if schedule_kind == "interval":
        sec = _parse_duration_seconds(expr)
        if sec is None or sec <= 0:
            return None
        return current + max(1.0, sec)

    if schedule_kind == "daily":
        m = _DAILY_HM_RE.match(expr)
        if not m:
            return None
        hour, minute, second = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        if hour > 23 or minute > 59 or second > 59:
            return None
        base_dt = datetime.fromtimestamp(current)
        target_dt = base_dt.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if target_dt.timestamp() <= current + 0.5:
            target_dt = target_dt + timedelta(days=1)
        return target_dt.timestamp()

    if schedule_kind == "once":
        if expr.startswith("+") or expr.endswith(("s", "m", "h", "d", "S", "M", "H", "D")):
            sec = _parse_duration_seconds(expr)
            if sec is not None and sec > 0:
                return current + sec
        # Numeric check (heuristic: > 1_000_000_000 = unix epoch, else relative seconds)
        try:
            num = float(expr)
            if num > 1_000_000_000:
                return num
            if num > 0:
                return current + num
        except ValueError:
            pass
        # ISO datetime
        try:
            clean_iso = expr.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_iso)
            return dt.timestamp()
        except ValueError:
            return None

    return None


class TaskScheduler:
    """Lightweight SQLite-backed asyncio scheduler for zero-token reminders & fast-model summaries."""

    def __init__(
        self,
        backend: MemoryBackend,
        llm_router: LLMRouter,
        config: AppConfig,
        *,
        on_notice: Optional[NoticeCallback] = None,
        on_soft_alert: Optional[SoftAlertCallback] = None,
    ) -> None:
        self.backend = backend
        self.llm_router = llm_router
        self.config = config
        self.on_notice = on_notice
        self.on_soft_alert = on_soft_alert
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if not self.config.scheduler.enabled:
            return
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "TaskScheduler started (poll_interval=%.1fs)",
            self.config.scheduler.poll_interval_sec,
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _poll_loop(self) -> None:
        interval = max(1.0, float(self.config.scheduler.poll_interval_sec))
        while not self._stop_event.is_set():
            try:
                await self.run_due_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("TaskScheduler tick error (isolated): %s", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def run_due_once(
        self,
        *,
        now: Optional[float] = None,
    ) -> List[OutputProactiveNoticePayload]:
        """Evaluate and fire all currently due active scheduled tasks (anti-spam protected)."""
        async with self._lock:
            current = now if now is not None else time.time()
            due_tasks = await self.backend.list_due_tasks(now=current)
            emitted: List[OutputProactiveNoticePayload] = []

            for task in due_tasks:
                overdue = current - task.next_run_at
                if task.schedule_kind == "once" and overdue > 86400.0:
                    logger.info(
                        "Skipping stale (>24h overdue) one-shot task %s (%s)",
                        task.task_id,
                        task.title,
                    )
                    await self.backend.update_scheduled_task(
                        task.task_id,
                        status="completed",
                        last_run_at=current,
                    )
                    continue

                notice = await self._execute_task(task, now=current)
                if task.schedule_kind == "once":
                    await self.backend.update_scheduled_task(
                        task.task_id,
                        status="completed",
                        last_run_at=current,
                    )
                else:
                    # Coalesce missed recurring cycles into a single next run strictly after `current`
                    next_ts = compute_next_run_at(
                        task.schedule_kind, task.schedule_expr, now=current
                    )
                    if next_ts is None:
                        await self.backend.update_scheduled_task(
                            task.task_id,
                            status="completed",
                            last_run_at=current,
                        )
                    else:
                        await self.backend.update_scheduled_task(
                            task.task_id,
                            status="active",
                            next_run_at=next_ts,
                            last_run_at=current,
                        )

                if notice is not None:
                    emitted.append(notice)
                    if self.on_notice is not None:
                        try:
                            await self.on_notice(notice)
                        except Exception as exc:
                            logger.warning("Failed dispatching proactive notice: %s", exc)

            return emitted

    async def _execute_task(
        self,
        task: ScheduledTaskRecord,
        *,
        now: float,
    ) -> Optional[OutputProactiveNoticePayload]:
        try:
            payload = json.loads(task.payload_json or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except json.JSONDecodeError:
            payload = {}

        if task.task_type == "reminder":
            msg = str(payload.get("message") or payload.get("content") or task.title).strip()
            content = msg or task.title
        else:
            content = await self._generate_llm_summary(task, payload, now=now)

        created_iso = datetime.fromtimestamp(now).isoformat(timespec="seconds")
        return OutputProactiveNoticePayload(
            notice_id=new_notice_id(),
            task_id=task.task_id,
            kind=task.task_type,
            title=task.title,
            content=content,
            created_at=created_iso,
        )

    async def _generate_llm_summary(
        self,
        task: ScheduledTaskRecord,
        payload: dict,
        *,
        now: float,
    ) -> str:
        episodes = await self.backend.list_episodes(status="active", limit=12)
        works = await self.backend.list_work_history(limit=8)

        ep_lines = [
            f"- [{ep.kind}] {ep.summary}" for ep in episodes
        ] or ["（暂无今日桌面或情景摘要）"]
        work_lines = [
            f"- {w.goal} -> {w.result_summary} ({w.status}, {w.step_count}步)"
            for w in works
        ] or ["（暂无工具执行记录）"]

        instruction = str(payload.get("instruction") or payload.get("message") or task.title)
        prompt = (
            f"请按以下指令为用户提供一份简短友好的主动摘要（3–5 句话，直接输出正文，不要套话）：\n"
            f"指令：{instruction}\n\n"
            f"【近期工作足迹与情景记忆】\n" + "\n".join(ep_lines) + "\n\n"
            f"【近期任务执行记录】\n" + "\n".join(work_lines)
        )
        profile_name = self.config.scheduler.summary_profile
        if profile_name not in self.llm_router._config.profiles:
            profile_name = self.llm_router.active_profile

        try:
            res = await self.llm_router.complete_with_tools(
                [
                    {
                        "role": "system",
                        "content": "你是启明星定时汇报助手。请用自然简洁的中文总结重点，不要编造未发生的内容。",
                    },
                    {"role": "user", "content": prompt},
                ],
                [],
                profile_name=profile_name,
            )
            u = res.usage
            if u.total_tokens > 0 or u.prompt_tokens > 0 or u.completion_tokens > 0:
                await self.backend.add_usage_record(
                    UsageLedgerRecord(
                        session_id="__scheduler__",
                        turn_id=task.task_id,
                        profile=u.profile or profile_name,
                        model=u.model,
                        prompt_tokens=u.prompt_tokens,
                        prompt_cache_hit_tokens=u.prompt_cache_hit_tokens,
                        completion_tokens=u.completion_tokens,
                        estimated_cost_cny=u.estimated_cost_cny,
                        created_at=now,
                    )
                )
                await self._check_soft_alert(u.total_tokens)

            cleaned = _EMOTION_TAG_RE.sub("", (res.content or "").strip())
            return cleaned or f"定时摘要「{task.title}」已生成，但内容为空。"
        except Exception as exc:
            logger.warning("llm_summary task=%s failed: %s", task.task_id, exc)
            return f"定时摘要任务「{task.title}」执行时遇到模型异常（{type(exc).__name__}），请稍后再试。"

    async def _check_soft_alert(self, turn_tokens: int) -> None:
        alert_cfg = self.config.tools.usage_alert
        if not alert_cfg.enabled or self.on_soft_alert is None:
            return
        lt = time.localtime()
        day_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        summary = await self.backend.get_usage_summary(since_ts=day_start)
        reasons: List[str] = []
        if alert_cfg.turn_warn_tokens > 0 and turn_tokens >= alert_cfg.turn_warn_tokens:
            reasons.append(f"定时摘要消耗 {turn_tokens} tokens（提醒阈值 {alert_cfg.turn_warn_tokens}）")
        if alert_cfg.daily_warn_cny > 0 and summary.total_cost_cny >= alert_cfg.daily_warn_cny:
            reasons.append(
                f"今日累计估算 ¥{summary.total_cost_cny:.4f}（提醒阈值 ¥{alert_cfg.daily_warn_cny:.2f}）"
            )
        if reasons:
            msg = (
                f"用量软提醒（定时任务）：{'；'.join(reasons)}。"
                f"当前缓存命中率 {summary.cache_hit_rate * 100:.1f}%，已按策略继续执行。"
            )
            await self.on_soft_alert(msg)
