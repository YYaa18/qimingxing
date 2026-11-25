from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest

from brain.config import AppConfig, ContextBudgetConfig
from brain.context_builder import ContextBuilder
from brain.context_store import ContextStore
from brain.llm_router import _parse_usage
from brain.memory.models import MemoryFact
from brain.memory.sqlite_store import SQLiteMemoryStore
from brain.tools.base import Tool, ToolExecutionContext, ToolResult
from brain.tools.registry import ToolRegistry
from protocol.models import RiskLevel


class _GuardTool(Tool):
    default_risk: RiskLevel = "safe"
    parameters_schema = {
        "type": "object",
        "properties": {
            "z": {"type": "string"},
            "a": {"type": "integer"},
        },
    }

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"guard {name}"

    async def execute(self, arguments, ctx):
        return ToolResult(ok=True, output="ok", summary="ok")


def _facts() -> List[MemoryFact]:
    return [
        MemoryFact(fact_id="f2", category="tech_stack", content="使用 SQLite WAL", importance=0.9, updated_at=2.0),
        MemoryFact(fact_id="f1", category="preference", content="回答直接给结论", importance=0.9, updated_at=2.0),
        MemoryFact(fact_id="f3", category="project", content="启明星使用 SwiftUI 壳", importance=0.8, updated_at=3.0),
    ]


def _build(builder: ContextBuilder, user_text: str, facts: List[MemoryFact], tools_json: str) -> Any:
    return builder.build(
        user_text=user_text, user_content=user_text,
        core_facts=facts, rolling_summary="【已完成】稳定摘要\n【技术决策】SQLite WAL",
        recent_messages=[
            {"role": "user", "content": "上一轮问题"},
            {"role": "assistant", "content": "上一轮回答"},
        ],
        recalled_memories=[], context_store=ContextStore(),
        system_base_prompt="L0 固定系统提示",
        tools_tokens=0,
    )


def _tools_bytes(registry: ToolRegistry) -> str:
    return json.dumps(registry.to_openai_tools(), ensure_ascii=False, sort_keys=True)


def test_pch01_static_prefix_summary_and_tools_are_byte_stable() -> None:
    registry = ToolRegistry([_GuardTool("shell_exec"), _GuardTool("web_fetch"), _GuardTool("ask_user")])
    builder = ContextBuilder(ContextBudgetConfig())
    tools_bytes = _tools_bytes(registry)
    first = _build(builder, "第 N 轮问题", _facts(), tools_bytes)
    second = _build(builder, "第 N+1 轮问题", _facts(), tools_bytes)

    assert first.messages[0]["content"] == second.messages[0]["content"]
    assert first.stats.static_prefix_hash == second.stats.static_prefix_hash
    assert first.messages[1]["content"] == second.messages[1]["content"]
    assert _tools_bytes(registry) == tools_bytes


def test_pch02_tool_registration_and_fact_order_are_immune() -> None:
    names = ["web_fetch", "shell_exec", "ask_user"]
    a = ToolRegistry([_GuardTool(n) for n in names])
    b = ToolRegistry([_GuardTool(n) for n in reversed(names)])
    assert _tools_bytes(a) == _tools_bytes(b)

    builder = ContextBuilder(ContextBudgetConfig())
    facts_a = _facts()
    facts_b = list(reversed(facts_a))
    first = _build(builder, "同一轮问题", facts_a, _tools_bytes(a))
    second = _build(builder, "同一轮问题", facts_b, _tools_bytes(b))
    assert first.messages[0]["content"] == second.messages[0]["content"]
    assert first.stats.static_prefix_hash == second.stats.static_prefix_hash
    assert _tools_bytes(a) == _tools_bytes(b)


def test_pch03_usage_normalization_prefers_all_provider_fields_and_clamps() -> None:
    deepseek = _parse_usage(
        {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 800, "completion_tokens": 10},
        "deepseek-flash", "p",
    )
    openai = _parse_usage(
        {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 600}, "completion_tokens": 10},
        "gpt-4o", "p",
    )
    anthropic = _parse_usage(
        {"prompt_tokens": 1000, "cache_read_input_tokens": 500, "completion_tokens": 10},
        "claude", "p",
    )
    clamped = _parse_usage(
        {"prompt_tokens": 100, "prompt_cache_hit_tokens": 999, "completion_tokens": 1},
        "deepseek-flash", "p",
    )
    assert deepseek.cached_input_tokens == 800
    assert openai.cached_input_tokens == 600
    assert anthropic.cached_input_tokens == 500
    assert clamped.cached_input_tokens == 100


@pytest.mark.asyncio
async def test_pch03_old_ledger_upgrade_and_jsonl_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE usage_ledger (
               ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
               session_id TEXT NOT NULL DEFAULT '',
               turn_id TEXT NOT NULL DEFAULT '',
               profile TEXT NOT NULL DEFAULT '',
               model TEXT NOT NULL DEFAULT '',
               prompt_tokens INTEGER NOT NULL DEFAULT 0,
               prompt_cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
               completion_tokens INTEGER NOT NULL DEFAULT 0,
               estimated_cost_cny REAL NOT NULL DEFAULT 0.0,
               created_at REAL NOT NULL
           )"""
    )
    conn.execute(
        "INSERT INTO usage_ledger (session_id, turn_id, profile, model, prompt_tokens, "
        "prompt_cache_hit_tokens, completion_tokens, estimated_cost_cny, created_at) "
        "VALUES ('s','t','p','deepseek-flash',1000,400,50,0.1,1.0)"
    )
    conn.commit()
    conn.close()

    store = SQLiteMemoryStore(db)
    await store.initialize()
    records = await store.list_usage_records(limit=10)
    assert len(records) == 1
    assert records[0].prompt_tokens == 1000
    assert records[0].cached_input_tokens == 400
    summary = await store.get_usage_summary()
    assert summary.cached_input_tokens == 400
    slots = await store.get_usage_by_slot()
    assert slots and slots[0].cached_input_tokens == 400
    with sqlite3.connect(db) as check:
        cols = {r[1] for r in check.execute("PRAGMA table_info(usage_ledger)")}
    assert "cached_input_tokens" in cols

    snapshot = tmp_path / "snap.jsonl"
    await store.export_snapshot(snapshot)
    await store.close()

    import_store = SQLiteMemoryStore(tmp_path / "import.db")
    await import_store.initialize()
    await import_store.import_snapshot(snapshot)
    imported = await import_store.list_usage_records(limit=10)
    assert imported[0].cached_input_tokens == 400
    await import_store.close()

    # Old snapshot without the field must default to 0, not crash.
    old_snap = tmp_path / "old_snap.jsonl"
    old_snap.write_text(json.dumps({
        "_type": "usage", "ledger_id": 77, "session_id": "s", "turn_id": "old",
        "profile": "p", "provider": "deepseek", "slot": "chat", "model": "deepseek-flash",
        "prompt_tokens": 100, "prompt_cache_hit_tokens": 30,
        "completion_tokens": 10, "estimated_cost_cny": 0.0, "created_at": 2.0,
    }) + "\n", encoding="utf-8")
    legacy_store = SQLiteMemoryStore(tmp_path / "legacy.db")
    await legacy_store.initialize()
    await legacy_store.import_snapshot(old_snap)
    legacy = await legacy_store.list_usage_records(limit=10)
    assert legacy[0].cached_input_tokens == 0
    await legacy_store.close()
