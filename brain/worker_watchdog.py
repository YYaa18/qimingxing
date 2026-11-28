from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

from protocol.models import TaskWatchdogAlertPayload

from brain.memory.models import BackgroundTaskRecord
from brain.watchdog import SemanticWatchdog, WatchdogDecision

logger = logging.getLogger("qimingxing.worker.watchdog")


class WatchdogMixin:
    """Watchdog loop + decisions (moved verbatim from BackgroundTaskRunner)."""

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

