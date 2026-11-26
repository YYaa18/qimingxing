from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from brain.config import AppConfig, ModelRoutingConfig, ProfileConfig, SlotRouteConfig
from brain.memory.models import BackgroundTaskRecord
from brain.memory.sqlite_store import SQLiteMemoryStore
from brain.tools import build_default_tool_registry
from brain.worker import (
    WorkerMandate,
    build_router_registry,
    build_worker_registry,
    path_within,
    validate_tool_allowed,
)


def test_router_and_worker_tool_surfaces_are_physically_split() -> None:
    cfg = AppConfig()
    full = build_default_tool_registry(cfg)
    router = build_router_registry(full)
    names = {tool.name for tool in router.list_tools()}
    assert "task_dispatch" in names
    assert "macos_calendar_create" in names
    assert "shell_exec" not in names
    assert "web_fetch" not in names
    assert "bg_task_submit" not in names
    assert not any(name.startswith("mcp__") for name in names)

    research = {tool.name for tool in build_worker_registry(full, ["web_research"]).list_tools()}
    assert {"web_search", "web_fetch", "web_image_search"}.issubset(research)
    dev = {tool.name for tool in build_worker_registry(full, ["dev_ops"]).list_tools()}
    assert "shell_exec" in dev
    office = {tool.name for tool in build_worker_registry(full, ["macos_office"]).list_tools()}
    assert "macos_mail_draft" in office


def test_worker_mandate_path_and_tool_scope() -> None:
    mandate = WorkerMandate.from_dict({
        "allowed_packs": ["dev_ops"],
        "writable_paths": ["/tmp/qmx_mandate"],
    })
    assert path_within("/tmp/qmx_mandate/report.md", mandate.writable_paths)
    assert not path_within("/tmp/elsewhere/report.md", mandate.writable_paths)
    assert validate_tool_allowed(
        tool_name="macos_notes_create",
        arguments={"title": "x"},
        mandate=mandate,
        allowed_tools={"shell_exec"},
    ) == "工具 `macos_notes_create` 不在当前 Worker Capability Pack 内"
    assert validate_tool_allowed(
        tool_name="task_inspect",
        arguments={},
        mandate=mandate,
        allowed_tools={"task_inspect"},
    ) is None


@pytest.mark.asyncio
async def test_background_task_worker_fields_inbox_and_locks(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "orch.db")
    await store.initialize()
    task = BackgroundTaskRecord(
        task_id="bg_orch",
        title="调研",
        goal_prompt="查资料",
        source_session_id="main",
        worker_session_id="worker_s",
        origin="explicit_user",
        mandate={"allowed_packs": ["web_research"], "writable_paths": []},
        checkpoint={"step": 2},
        result_refs=["ref:bg_orch:report"],
        last_heartbeat_at=1.0,
    )
    await store.add_background_task(task)
    loaded = await store.get_background_task("bg_orch")
    assert loaded is not None
    assert loaded.worker_session_id == "worker_s"
    assert loaded.mandate["allowed_packs"] == ["web_research"]
    assert loaded.checkpoint["step"] == 2
    assert loaded.result_refs == ["ref:bg_orch:report"]

    inbox = await store.add_task_inbox("bg_orch", sender="user_followup", content="换成人民币")
    assert inbox.id > 0
    items = await store.consume_task_inbox("bg_orch")
    assert [i.content for i in items] == ["换成人民币"]
    assert await store.consume_task_inbox("bg_orch") == []

    assert await store.acquire_resource_lock("path:/tmp/a", "t1", mode="shared")
    assert await store.acquire_resource_lock("path:/tmp/a", "t2", mode="shared")
    assert not await store.acquire_resource_lock("path:/tmp/a", "t3", mode="exclusive")
    await store.release_resource_lock("path:/tmp/a", "t2")
    assert not await store.acquire_resource_lock("path:/tmp/a", "t3", mode="exclusive")
    await store.release_resource_lock("path:/tmp/a", "t1")
    assert await store.acquire_resource_lock("path:/tmp/a", "t3", mode="exclusive")
    await store.close()


@pytest.mark.asyncio
async def test_old_background_table_migrates_new_worker_columns(tmp_path: Path) -> None:
    db = tmp_path / "old_bg.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE background_tasks (
            task_id TEXT PRIMARY KEY, title TEXT NOT NULL, goal_prompt TEXT NOT NULL,
            source_session_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'queued',
            progress_pct INTEGER NOT NULL DEFAULT 0, current_step TEXT NOT NULL DEFAULT '',
            result_summary TEXT NOT NULL DEFAULT '', artifact_path TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '', slot_used TEXT NOT NULL DEFAULT 'bg_task',
            created_at REAL NOT NULL, started_at REAL NULL, finished_at REAL NULL
        )"""
    )
    conn.commit()
    conn.close()
    store = SQLiteMemoryStore(db)
    await store.initialize()
    with sqlite3.connect(db) as check:
        cols = {r[1] for r in check.execute("PRAGMA table_info(background_tasks)")}
    assert {"worker_session_id", "mandate_json", "checkpoint_json", "result_refs_json"} <= cols
    await store.close()


def test_worker_model_slot_falls_back_to_chat() -> None:
    cfg = AppConfig(
        has_explicit_routing=True,
        profiles={"routed_chat": ProfileConfig(name="routed_chat", label="Routed", provider="mock", model="m")},
        model_routing=ModelRoutingConfig(
            chat=SlotRouteConfig(provider="deepseek", model="deepseek-flash"),
            worker=SlotRouteConfig(provider="", model=""),
        ),
    )
    cfg.providers["deepseek"].api_key = "k"
    assert cfg.resolve_slot_profile("worker").model == "deepseek-flash"
