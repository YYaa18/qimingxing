from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List

import httpx
import pytest
from fastapi.testclient import TestClient

from brain.config import AppConfig, ContextBudgetConfig, ProfileConfig
from brain.context_budget import resolve_context_budget
from brain.context_builder import ContextBuilder
from brain.context_store import ContextStore
from brain.llm_router import LLMCompletionResult, LLMRouter, ToolCallRequest
from brain.main import create_app
from brain.memory.extractor import _clip_structured_summary
from brain.memory.models import MemoryFact, MemorySkill, RetrievedMemory
from brain.model_config_store import (
    ModelConfigStore,
    ModelStoreConflictError,
    ModelStoreValidationError,
)
from brain.model_discovery import discover_provider_models
from brain.prompts import BASE_SYSTEM_PROMPT
from brain.tools.base import Tool, ToolExecutionContext, ToolResult
from brain.tools.executor import ToolExecutor
from brain.tools.registry import ToolRegistry
from protocol.models import RiskLevel


def _budget_cfg(tier: int = 32000) -> AppConfig:
    cfg = AppConfig()
    cfg.context_budget = ContextBudgetConfig(input_tier_tokens=tier, request_max_tokens=8192)
    return cfg


def test_ctx04_single_l3_item_is_capped() -> None:
    cfg = _budget_cfg()
    budget = resolve_context_budget(
        profile=ProfileConfig(
            name="p", label="P", provider="openai_compatible",
            provider_id="deepseek", model="deepseek-flash",
            context_window=1_048_576, max_output_tokens=32_768,
        ),
        config=cfg, system_prompt=BASE_SYSTEM_PROMPT, tools_schema=[],
        has_facts=True, has_summary=True,
    )
    skills = [
        MemorySkill(
            skill_id=f"s{i}", name="超长技能" * 200, trigger_intent="触发",
            steps_json=json.dumps(["步骤" * 200 for _ in range(5)], ensure_ascii=False),
        )
        for i in range(5)
    ]
    built = ContextBuilder(cfg.context_budget).build(
        user_text="处理", user_content="处理", core_facts=[],
        rolling_summary="", recent_messages=[], recalled_memories=[],
        context_store=ContextStore(), recalled_skills=skills,
        resolved_budget=budget,
    )
    assert built.stats.l3_skills_tokens <= budget.skills_cap


def test_ctx05_image_turn_does_not_shift_calibration() -> None:
    builder = ContextBuilder(ContextBudgetConfig())
    for _ in range(3):
        builder.observe_prompt_usage(1000, 1000)
    content = [
        {"type": "text", "text": "看屏幕"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
    ]
    built = builder.build(
        user_text="看屏幕", user_content=content, core_facts=[],
        rolling_summary="", recent_messages=[], recalled_memories=[],
        context_store=ContextStore(),
    )
    builder.observe_prompt_usage(
        built.stats.total_with_tools,
        built.stats.total_with_tools + 2400,
        has_images=built.stats.has_image_input,
    )
    assert built.stats.has_image_input is True
    assert builder.meter.scale == 1.0


def test_cmp07_structured_summary_keeps_headings_and_backfills() -> None:
    previous = (
        "【已完成/已确认】\nA\n【关键文件与路径】\nB\n【技术决策与原因】\nC\n"
        "【待办/未解决问题】\nD\n【用户偏好与约束】\n不吃香菜"
    )
    new = (
        "【已完成/已确认】\n" + "x" * 900 + "\n"
        "【关键文件与路径】\nbrain/hub.py\n"
        "【技术决策与原因】\n放弃 Keychain\n"
        "【待办/未解决问题】\n修复 symlink\n"
    )
    clipped = _clip_structured_summary(new, previous, 700)
    for heading in (
        "【已完成/已确认】", "【关键文件与路径】", "【技术决策与原因】",
        "【待办/未解决问题】", "【用户偏好与约束】",
    ):
        assert heading in clipped
    assert "不吃香菜" in clipped
    assert len(clipped) <= 700


class _DummyTool(Tool):
    default_risk: RiskLevel = "safe"
    parameters_schema = {"type": "object", "properties": {}}
    name = "shell_exec"
    description = "shell"

    async def execute(self, arguments, ctx):
        return ToolResult(ok=True, output="x" * 10000, summary="ok")


class _ToolRouter(LLMRouter):
    def __init__(self, config: AppConfig, steps: int) -> None:
        super().__init__(config)
        self.steps = steps
        self.seen: List[List[Dict[str, Any]]] = []

    async def complete_with_tools(self, messages, tools, *, profile_name=None, on_text_delta=None):
        self.seen.append([dict(m) for m in messages])
        if len(self.seen) <= self.steps:
            return LLMCompletionResult(tool_calls=[
                ToolCallRequest(id=f"c{len(self.seen)}", name="shell_exec", arguments_json="{}")
            ])
        return LLMCompletionResult(content="done")


@pytest.mark.asyncio
async def test_tlg01_wrapup_is_folded() -> None:
    cfg = AppConfig(default_profile="m", profiles={
        "m": ProfileConfig(name="m", label="m", provider="mock", model="m")
    })
    router = _ToolRouter(cfg, 8)
    executor = ToolExecutor(router)
    ctx = ToolExecutionContext(turn_id="t", session_id="s", config=cfg)
    await executor.run(
        messages=[{"role": "system", "content": "s" * 20000}, {"role": "user", "content": "start"}],
        registry=ToolRegistry([_DummyTool()]), ctx=ctx,
        max_steps=8, max_context_tokens=23000,
    )
    totals = [sum(__import__("brain.context_builder", fromlist=["estimate_message_tokens"]).estimate_message_tokens(m) for m in msgs) for msgs in router.seen]
    assert max(totals) <= 23000
    tool_msgs = [m for m in router.seen[-1] if m.get("role") == "tool"]
    assert any("已折叠" in str(m.get("content")) for m in tool_msgs)


def test_tlg03_fold_placeholder_keeps_tool_and_args() -> None:
    executor = ToolExecutor(None)
    messages = [{"role": "user", "content": "start"}]
    for i in range(3):
        messages.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": f"c{i}", "type": "function",
            "function": {"name": "web_fetch", "arguments": '{"url":"https://example.com/config"}'},
        }]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 10000})
    folded = executor._fold_old_tool_outputs(messages, max_tokens=5000, keep_last=2)
    content = str(folded[2]["content"])
    assert "web_fetch" in content
    assert "example.com/config" in content
    assert "work_history" in content


def test_mdb01_migration_creates_0600_backup(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("default_profile: fast\n", encoding="utf-8")
    store = ModelConfigStore(tmp_path / "models.db", cfg_path)
    store.initialize(AppConfig())
    backup = cfg_path.with_name(cfg_path.name + ".bak-models")
    assert backup.exists()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_mdb02_startup_falls_back_to_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "default_profile: mock\n"
        "profiles:\n  mock:\n    provider: mock\n    model: mock-v1\n"
        f"memory:\n  sqlite_path: {tmp_path / 'fallback.db'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("QIMINGXING_CONFIG", str(cfg_path))
    def boom(self, cfg=None):
        raise OSError("disk full during migration")
    monkeypatch.setattr(ModelConfigStore, "initialize", boom)
    app = create_app(settings_config_path=cfg_path)
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["model_store_mode"] == "yaml_fallback"


def test_mdb03_routed_provider_delete_conflicts(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "models.db")
    store.initialize(AppConfig())
    store.apply_provider_patch({"custom_x": {
        "label": "X", "base_url": "https://example.com/v1", "api_key": "k", "models": ["m"],
    }})
    store.apply_routing_patch({"chat": {"provider": "custom_x", "model": "m"}})
    with pytest.raises(ModelStoreConflictError):
        store.apply_provider_patch({"custom_x": {"delete": True}})
    assert store.routing()["chat"]["provider"] == "custom_x"


def test_mdb05_routing_extra_json_roundtrip(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "models.db")
    store.initialize(AppConfig())
    store.apply_provider_patch({"custom_e": {
        "models": ["m"],
        "model_capabilities": {"m": {
            "context_window": 32768, "max_output_tokens": 4096,
            "supports_tools": True, "supports_vision": False, "source": "manual",
        }},
    }})
    store.apply_routing_patch({"chat": {
        "provider": "custom_e", "model": "m",
        "extra": {"temperature": 0.3, "effort": "low", "max_output_override": 2048},
    }})
    with sqlite3.connect(store.db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(model_routing)")}
    assert "extra_json" in cols
    assert store.routing()["chat"]["extra"]["effort"] == "low"
    cfg = AppConfig()
    store.apply_to_config(cfg)
    assert cfg.profiles["routed_chat"].temperature == 0.3
    assert cfg.profiles["routed_chat"].request_max_tokens == 2048
    assert cfg.profiles["routed_chat"].reasoning_effort == "low"


@pytest.mark.asyncio
async def test_dsc01_ollama_show_and_preview_flag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "generativelanguage" in url:
            return httpx.Response(200, json={"models": [
                {"name": "models/gemini-3-flash-preview", "displayName": "Preview",
                 "inputTokenLimit": 1048576, "outputTokenLimit": 65536,
                 "supportedGenerationMethods": ["generateContent"]},
            ]})
        if "/api/tags" in url:
            return httpx.Response(200, json={"models": [{"name": "llama3.1:8b"}]})
        if "/api/show" in url:
            return httpx.Response(200, json={"model_info": {"llama.context_length": 8192}})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    gem = await discover_provider_models(
        provider_id="gemini", protocol="openai_compatible",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key="g", transport=transport,
    )
    assert gem[0]["is_preview"] is True
    oll = await discover_provider_models(
        provider_id="local_ollama", protocol="openai_compatible",
        base_url="http://127.0.0.1:11434/v1", api_key="", transport=transport,
    )
    assert oll[0]["context_window"] == 8192


@pytest.mark.asyncio
async def test_dsc04_dirty_discovery_falls_back_and_bad_caps_rejected(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{
            "id": "deepseek-flash", "context_window": "invalid", "max_output_tokens": None,
        }]})

    result = await discover_provider_models(
        provider_id="deepseek", protocol="openai_compatible",
        base_url="https://api.deepseek.com/v1", api_key="d",
        transport=httpx.MockTransport(handler),
    )
    assert result[0]["context_window"] > 0

    store = ModelConfigStore(tmp_path / "models.db")
    store.initialize(AppConfig())
    with pytest.raises(ModelStoreValidationError):
        store.apply_provider_patch({"custom_q": {
            "models": ["m"],
            "model_capabilities": {"m": {
                "context_window": 500, "max_output_tokens": 20000000,
                "source": "manual",
            }},
        }})


def _settings_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "default_profile: mock\n"
        "profiles:\n  mock:\n    provider: mock\n    model: mock-v1\n"
        f"memory:\n  sqlite_path: {tmp_path / 'memory.db'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("QIMINGXING_CONFIG", str(cfg_path))
    return create_app(settings_config_path=cfg_path), cfg_path


def test_settings_api_maps_cap_validation_to_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = _settings_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        r = client.post("/api/config/settings", json={"providers": {"custom_bad": {
            "label": "Bad", "base_url": "https://example.com/v1", "api_key": "k",
            "models": ["m"],
            "model_capabilities": {"m": {
                "context_window": 500, "max_output_tokens": 4096,
                "source": "manual",
            }},
        }}})
        assert r.status_code == 422, r.text
        assert "上下文" in r.json().get("detail", "")


def test_settings_api_maps_routed_delete_to_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = _settings_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        r1 = client.post("/api/config/settings", json={"providers": {"custom_used": {
            "label": "Used", "base_url": "https://example.com/v1", "api_key": "k",
            "models": ["m"],
            "model_capabilities": {"m": {
                "context_window": 32768, "max_output_tokens": 4096,
                "source": "manual",
            }},
        }}})
        assert r1.status_code == 200, r1.text
        r2 = client.post("/api/config/settings", json={"model_routing": {
            "chat": {"provider": "custom_used", "model": "m"},
        }})
        assert r2.status_code == 200, r2.text
        r3 = client.post("/api/config/settings", json={"providers": {
            "custom_used": {"delete": True},
        }})
        assert r3.status_code == 409, r3.text
        assert "请先切换路由" in r3.json().get("detail", "")
