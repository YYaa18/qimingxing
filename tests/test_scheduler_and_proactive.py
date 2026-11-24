from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from brain.config import AppConfig, ProfileConfig, SchedulerConfig
from brain.llm_router import LLMCompletionResult, LLMRouter, LLMUsage
from brain.memory.cli import main as cli_main
from brain.memory.models import MemoryEpisode, ScheduledTaskRecord, WorkHistoryRecord
from brain.memory.sqlite_store import SQLiteMemoryStore
from brain.scheduler import TaskScheduler, compute_next_run_at
from brain.tools.base import ToolExecutionContext
from brain.tools.scheduler_tool import CancelTaskTool, ListTasksTool, ScheduleTaskTool


class SummaryMockRouter(LLMRouter):
    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self.calls = 0

    async def complete_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        profile_name: Optional[str] = None,
        on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> LLMCompletionResult:
        self.calls += 1
        return LLMCompletionResult(
            content="今日你主要在开发 Qimingxing 阶段五调度与 MCP 模块，3 项关键任务均已顺利归档。",
            usage=LLMUsage(
                prompt_tokens=600,
                prompt_cache_hit_tokens=300,
                completion_tokens=60,
                total_tokens=660,
                estimated_cost_cny=0.002,
                model="deepseek-chat",
                profile="fast_mock",
            ),
        )


def test_compute_next_run_expressions() -> None:
    base = 1764000000.0
    assert compute_next_run_at("once", "+60s", now=base) == base + 60.0
    assert compute_next_run_at("once", "5m", now=base) == base + 300.0
    assert compute_next_run_at("interval", "10m", now=base) == base + 600.0
    daily_ts = compute_next_run_at("daily", "23:59", now=base)
    assert daily_ts is not None and daily_ts > base


async def test_scheduler_tools_persistence_and_snapshot_v5(tmp_path: Path) -> None:
    db1 = tmp_path / "sched.db"
    store1 = SQLiteMemoryStore(db1)
    await store1.initialize()

    ctx = ToolExecutionContext(
        turn_id="t_sched",
        session_id="s1",
        config=AppConfig(),
        memory_backend=store1,
    )
    res_add = await ScheduleTaskTool().execute(
        {
            "title": "喝水休息提醒",
            "task_type": "reminder",
            "schedule_kind": "once",
            "schedule_expr": "+1m",
            "message": "记得喝水站起来活动一下！",
        },
        ctx,
    )
    assert res_add.ok is True
    task_id = res_add.metadata["task_id"]

    res_list = await ListTasksTool().execute({"status": "active"}, ctx)
    assert "喝水休息提醒" in res_list.output
    await store1.close()

    # Reopen after restart -> persisted
    store2 = SQLiteMemoryStore(db1)
    await store2.initialize()
    loaded = await store2.get_scheduled_task(task_id)
    assert loaded is not None and loaded.status == "active"

    # Snapshot export/import v5 roundtrip
    snap = tmp_path / "snap_v5.jsonl"
    await store2.export_snapshot(snap)
    await store2.close()

    store3 = SQLiteMemoryStore(tmp_path / "sched_restored.db")
    await store3.initialize()
    await store3.import_snapshot(snap)
    restored = await store3.get_scheduled_task(task_id)
    assert restored is not None and restored.title == "喝水休息提醒"

    ctx3 = ToolExecutionContext(
        turn_id="t_cancel",
        session_id="s2",
        config=AppConfig(),
        memory_backend=store3,
    )
    res_cancel = await CancelTaskTool().execute({"title_keyword": "喝水"}, ctx3)
    assert res_cancel.ok is True
    after_cancel = await store3.get_scheduled_task(task_id)
    assert after_cancel is not None and after_cancel.status == "cancelled"
    await store3.close()


async def test_scheduler_reminder_zero_token_summary_soft_alert_and_antispam(
    tmp_path: Path,
) -> None:
    store = SQLiteMemoryStore(tmp_path / "exec_sched.db")
    await store.initialize()

    cfg = AppConfig(
        default_profile="fast_mock",
        profiles={
            "fast_mock": ProfileConfig(
                name="fast_mock", label="Fast", provider="mock", model="deepseek-chat"
            )
        },
        scheduler=SchedulerConfig(enabled=True, poll_interval_sec=5.0, summary_profile="fast_mock"),
    )
    cfg.tools.usage_alert.daily_warn_cny = 0.0001
    cfg.tools.usage_alert.hard_block = False

    router = SummaryMockRouter(cfg)
    notices = []
    alerts: List[str] = []

    async def on_notice(n):
        notices.append(n)

    async def on_alert(msg: str):
        alerts.append(msg)

    scheduler = TaskScheduler(
        store, router, cfg, on_notice=on_notice, on_soft_alert=on_alert
    )

    now = time.time()
    # 1) Due reminder -> MUST emit with 0 LLM calls and 0 usage ledger records
    await store.add_scheduled_task(
        ScheduledTaskRecord(
            task_id="task_rem_1",
            title="站立拉伸",
            task_type="reminder",
            schedule_kind="once",
            schedule_expr="+10s",
            payload_json=json.dumps({"message": "该起身拉伸两分钟啦"}, ensure_ascii=False),
            next_run_at=now - 5.0,
            status="active",
        )
    )
    fired = await scheduler.run_due_once(now=now)
    assert len(fired) == 1
    assert fired[0].kind == "reminder"
    assert "起身拉伸" in fired[0].content
    assert router.calls == 0
    assert (await store.get_usage_summary()).calls == 0
    rem_after = await store.get_scheduled_task("task_rem_1")
    assert rem_after is not None and rem_after.status == "completed"

    # 2) Seed episode & work history, run due llm_summary -> emits summary, logs usage, fires soft alert without blocking
    await store.add_episode(
        MemoryEpisode(
            episode_id="ep_sched_1",
            kind="desktop_footprint",
            summary="在 VS Code 编辑 scheduler.py",
        )
    )
    await store.add_work_history(
        WorkHistoryRecord(
            work_id="work_s1",
            session_id="sess_1",
            turn_id="t1",
            goal="测试定时摘要",
            result_summary="全部通过",
            step_count=1,
        )
    )
    await store.add_scheduled_task(
        ScheduledTaskRecord(
            task_id="task_sum_1",
            title="每日工作晚报",
            task_type="llm_summary",
            schedule_kind="interval",
            schedule_expr="600s",
            payload_json=json.dumps({"instruction": "总结工作进展"}, ensure_ascii=False),
            next_run_at=now - 36000.0,  # Simulate offline for 60 missed cycles -> must coalesce to ONE execution!
            status="active",
        )
    )
    # 3) Stale once task (> 24h overdue) -> must be skipped silently without spamming
    await store.add_scheduled_task(
        ScheduledTaskRecord(
            task_id="task_stale_1",
            title="昨天过期提醒",
            task_type="reminder",
            schedule_kind="once",
            schedule_expr="+10s",
            payload_json=json.dumps({"message": "过期提醒"}, ensure_ascii=False),
            next_run_at=now - 90000.0,
            status="active",
        )
    )

    fired2 = await scheduler.run_due_once(now=now)
    assert len(fired2) == 1
    assert fired2[0].task_id == "task_sum_1"
    assert fired2[0].kind == "llm_summary"
    assert "Qimingxing" in fired2[0].content
    assert router.calls == 1
    assert (await store.get_usage_summary()).calls == 1
    assert len(alerts) == 1 and "用量软提醒" in alerts[0]

    sum_after = await store.get_scheduled_task("task_sum_1")
    assert sum_after is not None and sum_after.next_run_at == now + 600.0
    stale_after = await store.get_scheduled_task("task_stale_1")
    assert stale_after is not None and stale_after.status == "completed"
    await store.close()


def test_cli_tasks_list_and_cancel(tmp_path: Path, capsys) -> None:
    import asyncio

    db_path = tmp_path / "cli_tasks.db"

    async def seed() -> None:
        st = SQLiteMemoryStore(db_path)
        await st.initialize()
        await st.add_scheduled_task(
            ScheduledTaskRecord(
                task_id="task_cli_1",
                title="CLI 测试提醒",
                task_type="reminder",
                schedule_kind="daily",
                schedule_expr="22:00",
                next_run_at=time.time() + 3600.0,
            )
        )
        await st.close()

    asyncio.run(seed())

    assert cli_main(["--db", str(db_path), "tasks"]) == 0
    assert "task_cli_1" in capsys.readouterr().out

    assert cli_main(["--db", str(db_path), "tasks", "--cancel", "task_cli_1"]) == 0
    assert "Cancelled task task_cli_1" in capsys.readouterr().out
