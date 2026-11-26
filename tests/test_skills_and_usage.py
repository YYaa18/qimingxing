from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

from fastapi.testclient import TestClient

from brain.config import AppConfig, ProfileConfig, SkillsConfig
from brain.context_builder import ContextBuilder
from brain.context_store import ContextStore
from brain.llm_router import LLMCompletionResult, LLMRouter, LLMUsage, ToolCallRequest
from brain.main import create_app
from brain.memory.cli import main as cli_main
from brain.memory.skills import SkillManager
from brain.memory.sqlite_store import SQLiteMemoryStore
from brain.tools import Tool, ToolExecutionContext, ToolRegistry, ToolResult, ToolTraceStep
from brain.tools.history_tool import WorkHistoryInspectTool
from protocol.models import RiskLevel


class SkillDistillMockRouter(LLMRouter):
    """Mocks both tool completion and background skill distillation."""

    def __init__(self, config: AppConfig, distill_payload: str) -> None:
        super().__init__(config)
        self.distill_payload = distill_payload
        self.tool_steps: List[LLMCompletionResult] = []

    async def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        profile_name: Optional[str] = None,
        *,
        json_mode: bool = False,
    ) -> AsyncIterator[str]:
        sys_txt = str(messages[0].get("content", ""))
        if "过程性技能" in sys_txt:
            yield self.distill_payload
            return
        async for tok in super().stream_chat(
            messages, profile_name=profile_name, json_mode=json_mode
        ):
            yield tok

    async def complete_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        profile_name: Optional[str] = None,
        on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> LLMCompletionResult:
        res = (
            self.tool_steps.pop(0)
            if self.tool_steps
            else LLMCompletionResult(content="[EMOTION:happy]\n已完成。")
        )
        if res.content and on_text_delta is not None:
            await on_text_delta(res.content)
        return res


async def test_skill_learn_recall_feedback_and_snapshot_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "skills.db"
    store1 = SQLiteMemoryStore(db_path)
    await store1.initialize()

    distill_json = json.dumps(
        {
            "action": "UPSERT",
            "target_skill_id": None,
            "name": "检查 Git 仓库与 Python 版本",
            "trigger_intent": "查看 Git 状态与 Python 环境版本",
            "tool_preference": "shell",
            "prerequisites": "项目目录已初始化 git",
            "steps": ["运行 git status 确认改动", "运行 python3 --version 核对版本"],
        },
        ensure_ascii=False,
    )
    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="m")},
    )
    router = SkillDistillMockRouter(cfg, distill_json)
    mgr = SkillManager(store1, router, SkillsConfig(auto_learn=True, learn_profile="mock_local"))

    skill = await mgr.learn_from_trace(
        user_text="帮我检查一下 Git 状态和 Python 版本",
        trace_steps=[
            ToolTraceStep(1, "shell_exec", {"command": "git status"}, "safe", "ok", "clean"),
            ToolTraceStep(2, "shell_exec", {"command": "python3 --version"}, "confirm", "ok", "Python 3.12"),
        ],
        final_summary="仓库干净，Python 3.12.16。",
    )
    assert skill is not None
    await store1.close()

    # Reopen after simulated restart and verify skill recall + L3 injection + L0/L1 prefix stability
    store2 = SQLiteMemoryStore(db_path)
    await store2.initialize()
    mgr2 = SkillManager(store2, router)
    recalled = await mgr2.recall_skills("再帮我查一下 Python 版本和 Git 状态")
    assert len(recalled) == 1
    assert recalled[0].skill_id == skill.skill_id

    builder = ContextBuilder()
    without_skill = builder.build(
        user_text="再帮我查一下 Git 状态",
        user_content="再帮我查一下 Git 状态",
        core_facts=[],
        rolling_summary="",
        recent_messages=[],
        recalled_memories=[],
        context_store=ContextStore(),
    )
    with_skill = builder.build(
        user_text="再帮我查一下 Git 状态",
        user_content="再帮我查一下 Git 状态",
        core_facts=[],
        rolling_summary="",
        recent_messages=[],
        recalled_memories=[],
        context_store=ContextStore(),
        recalled_skills=recalled,
    )
    assert without_skill.stats.static_prefix_hash == with_skill.stats.static_prefix_hash
    assert "检查 Git 仓库与 Python 版本" in with_skill.messages[-1]["content"]

    # Record 3 failures -> skill auto-deprecates and drops out of active search
    for _ in range(3):
        updated = await mgr2.record_feedback(skill.skill_id, success=False)
    assert updated is not None and updated.status == "deprecated"
    assert await mgr2.recall_skills("检查 Git 状态") == []

    # Export/import snapshot roundtrip
    snap = tmp_path / "snap_v4.jsonl"
    await store2.export_snapshot(snap)
    await store2.close()

    store3 = SQLiteMemoryStore(tmp_path / "restored.db")
    await store3.initialize()
    await store3.import_snapshot(snap)
    restored = await store3.get_skill(skill.skill_id)
    assert restored is not None
    assert restored.name == "检查 Git 仓库与 Python 版本"
    await store3.close()


class _SimpleSafeTool(Tool):
    name = "safe_echo"
    description = "echo text"
    default_risk: RiskLevel = "safe"
    parameters_schema = {"type": "object", "properties": {"msg": {"type": "string"}}}

    async def execute(self, arguments: Dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        return ToolResult(ok=True, output=f"echo:{arguments.get('msg', '')}", summary="已回显")


def test_work_history_and_usage_soft_alert_non_blocking(tmp_path: Path) -> None:
    db_path = tmp_path / "usage_work.db"
    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="deepseek-chat")},
    )
    cfg.memory.sqlite_path = str(db_path)
    cfg.memory.extraction.enabled = False
    # Extremely low threshold so soft alert fires, while hard_block stays False
    cfg.tools.usage_alert.daily_warn_cny = 0.0001
    cfg.tools.usage_alert.hard_block = False

    distill_json = json.dumps({"action": "SKIP"})
    router = SkillDistillMockRouter(cfg, distill_json)
    router.tool_steps = [
        LLMCompletionResult(
            tool_calls=[ToolCallRequest(id="u1", name="safe_echo", arguments_json='{"msg":"hi"}')],
            usage=LLMUsage(
                prompt_tokens=1000,
                prompt_cache_hit_tokens=800,
                completion_tokens=100,
                total_tokens=1100,
                estimated_cost_cny=0.002,
                model="deepseek-chat",
                profile="mock_local",
            ),
        ),
        LLMCompletionResult(
            content="[EMOTION:happy]\n工具完成，且未被软额度告警阻断。",
            usage=LLMUsage(
                prompt_tokens=1200,
                prompt_cache_hit_tokens=1000,
                completion_tokens=150,
                total_tokens=1350,
                estimated_cost_cny=0.002,
                model="deepseek-chat",
                profile="mock_local",
            ),
        ),
    ]

    app = create_app(config=cfg, llm_router=router)
    app.state.hub.tool_registry = ToolRegistry([_SimpleSafeTool(), WorkHistoryInspectTool()])

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "h_u",
                        "ts": 1,
                        "source": "body",
                        "type": "system.hello",
                        "payload": {"client_name": "body", "role": "body"},
                    }
                )
            )
            while json.loads(ws.receive_text())["type"] != "system.welcome":
                pass

            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "t_u",
                        "turn_id": "turn_usage",
                        "ts": 2,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "跑一次回显工具"},
                    }
                )
            )
            seen_types = []
            soft_alerts = []
            while True:
                ev = json.loads(ws.receive_text())
                seen_types.append(ev["type"])
                if ev["type"] == "system.error":
                    soft_alerts.append(ev["payload"])
                if ev["type"] == "output.turn_end":
                    assert ev["payload"]["finish_reason"] == "completed"
                    break

            assert any(a["code"] == "USAGE_SOFT_ALERT" and a["recoverable"] is True for a in soft_alerts)

        dbg = client.get("/api/memory/debug").json()
        assert dbg["work_history_count"] >= 1
        assert dbg["today_usage"]["calls"] == 2
        assert dbg["today_usage"]["cached_input_tokens"] == 1800
        assert dbg["today_usage"]["cache_hit_rate"] > 0.8


def test_cli_skills_history_and_usage(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "cli_v4.db"
    import asyncio
    from brain.memory.models import MemorySkill, UsageLedgerRecord, WorkHistoryRecord

    async def seed() -> None:
        st = SQLiteMemoryStore(db_path)
        await st.initialize()
        await st.upsert_skill(
            MemorySkill(
                skill_id="skill_cli_1",
                name="CLI 技能测试",
                trigger_intent="测试 CLI 技能命令",
                steps_json='["step 1"]',
            )
        )
        await st.add_work_history(
            WorkHistoryRecord(
                work_id="work_cli_1",
                session_id="sess_1",
                turn_id="t1",
                goal="测试工作历史",
                result_summary="全部通过",
                trace_json='[{"step":1,"tool":"safe_echo","status":"ok"}]',
                step_count=1,
            )
        )
        await st.add_usage_record(
            UsageLedgerRecord(
                session_id="sess_1",
                turn_id="t1",
                profile="fast_deepseek",
                model="deepseek-chat",
                prompt_tokens=500,
                prompt_cache_hit_tokens=400,
                completion_tokens=100,
                estimated_cost_cny=0.001,
            )
        )
        await st.close()

    asyncio.run(seed())

    assert cli_main(["--db", str(db_path), "skills", "list"]) == 0
    assert "skill_cli_1" in capsys.readouterr().out

    assert cli_main(["--db", str(db_path), "skills", "inspect", "--id", "skill_cli_1"]) == 0
    assert "CLI 技能测试" in capsys.readouterr().out

    assert cli_main(["--db", str(db_path), "history"]) == 0
    assert "work_cli_1" in capsys.readouterr().out

    assert cli_main(["--db", str(db_path), "history", "--id", "work_cli_1"]) == 0
    assert "safe_echo" in capsys.readouterr().out

    assert cli_main(["--db", str(db_path), "usage", "--days", "1"]) == 0
    out_usage = capsys.readouterr().out
    assert "hit_rate=80.0%" in out_usage

    assert cli_main(["--db", str(db_path), "skills", "delete", "--id", "skill_cli_1"]) == 0
    assert "Deprecated skill skill_cli_1" in capsys.readouterr().out


async def test_p4_03_pure_web_search_does_not_auto_learn_without_explicit_command(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "p4_03.db")
    await store.initialize()
    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="m")},
    )
    mgr = SkillManager(store, LLMRouter(cfg), SkillsConfig(auto_learn=True))

    search_only = [
        ToolTraceStep(1, "web_search", {"query": "Python 3.12"}, "safe", "ok", "5 hits"),
        ToolTraceStep(2, "web_fetch", {"url": "https://docs.python.org"}, "safe", "ok", "fetched"),
    ]
    assert mgr.should_learn("帮我查一下 Python 3.12 新特性", search_only) is False
    assert mgr.should_learn("记住这个操作流程，以后按这样查", search_only) is True

    shell_steps = [
        ToolTraceStep(1, "shell_exec", {"command": "git status"}, "safe", "ok", "clean"),
        ToolTraceStep(2, "shell_exec", {"command": "pytest -q"}, "confirm", "ok", "58 passed"),
    ]
    assert mgr.should_learn("跑一下状态和测试", shell_steps) is True
    await store.close()


def test_p4_04_per_profile_pricing_override() -> None:
    from brain.llm_router import estimate_cny_cost

    custom_prof = ProfileConfig(
        name="custom_paid",
        label="Custom",
        provider="openai_compatible",
        model="deepseek-chat",
        input_cny_per_1m=10.0,
        cache_hit_cny_per_1m=1.0,
        output_cny_per_1m=20.0,
    )
    # 1M total prompt (500k miss * 10 + 500k hit * 1) + 500k out * 20 = 5.0 + 0.5 + 10.0 = 15.5 CNY
    cost = estimate_cny_cost(
        "deepseek-chat",
        prompt_tokens=1_000_000,
        cache_hit_tokens=500_000,
        completion_tokens=500_000,
        profile=custom_prof,
    )
    assert abs(cost - 15.5) < 1e-9

    # Also verify load_config ignores legacy computer_use block cleanly & parses profile pricing overrides
    import yaml, tempfile
    from brain.config import load_config

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as tf:
        yaml.safe_dump(
            {
                "default_profile": "p1",
                "profiles": {
                    "p1": {
                        "label": "P1",
                        "provider": "openai_compatible",
                        "model": "m1",
                        "input_cny_per_1m": 4.5,
                        "cache_hit_cny_per_1m": 0.5,
                        "output_cny_per_1m": 12.0,
                    }
                },
                "tools": {"computer_use": {"enabled": False}},
            },
            tf,
        )
        cfg_path = Path(tf.name)
    loaded = load_config(cfg_path)
    cfg_path.unlink(missing_ok=True)
    assert not hasattr(loaded.tools, "computer_use")
    assert loaded.profiles["p1"].input_cny_per_1m == 4.5
    assert loaded.profiles["p1"].cache_hit_cny_per_1m == 0.5
    assert loaded.profiles["p1"].output_cny_per_1m == 12.0
