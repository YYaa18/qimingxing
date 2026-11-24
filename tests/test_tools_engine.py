from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi.testclient import TestClient

from brain.config import AppConfig, ProfileConfig
from brain.context_builder import ContextBuilder
from brain.context_store import ContextStore
from brain.llm_router import LLMCompletionResult, LLMRouter, LLMUsage, ToolCallRequest
from brain.main import create_app
from brain.memory.models import MemoryFact, MemorySkill
from brain.tools import (
    Tool,
    ToolExecutionContext,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
)
from protocol.models import RiskLevel


class DummyTool(Tool):
    def __init__(
        self,
        name: str,
        *,
        risk: RiskLevel = "safe",
        fail: bool = False,
    ) -> None:
        self.name = name
        self.description = f"Dummy tool {name}"
        self.default_risk = risk
        self.parameters_schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        self.fail = fail
        self.calls: List[Dict[str, Any]] = []

    async def execute(self, arguments: Dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        self.calls.append(arguments)
        if self.fail:
            return ToolResult(ok=False, output="boom", summary="模拟失败", status="error")
        return ToolResult(ok=True, output=f"ok:{self.name}:{arguments.get('x', '')}", summary="成功")


class ScriptedToolRouter(LLMRouter):
    def __init__(self, config: AppConfig, steps: List[LLMCompletionResult]) -> None:
        super().__init__(config)
        self._steps = list(steps)
        self.recorded_messages: List[List[Dict[str, Any]]] = []

    async def complete_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        profile_name: Optional[str] = None,
        on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> LLMCompletionResult:
        self.recorded_messages.append([dict(m) for m in messages])
        if not self._steps:
            res = LLMCompletionResult(content="[EMOTION:neutral]\n兜底结束。", finish_reason="stop")
        else:
            res = self._steps.pop(0)
        if res.content and on_text_delta is not None:
            await on_text_delta(res.content)
        return res


def test_tool_registry_deterministic_sorting_and_prefix_cache() -> None:
    reg = ToolRegistry([DummyTool("z_tool"), DummyTool("a_tool"), DummyTool("m_tool")])
    schemas = reg.to_openai_tools()
    assert [s["function"]["name"] for s in schemas] == ["a_tool", "m_tool", "z_tool"]

    builder = ContextBuilder()
    facts = [MemoryFact(fact_id="f1", category="tech_stack", content="Python 3.12", importance=0.9)]
    b1 = builder.build(
        user_text="普通问题 1",
        user_content="普通问题 1",
        core_facts=facts,
        rolling_summary="",
        recent_messages=[],
        recalled_memories=[],
        context_store=ContextStore(),
        recalled_skills=[],
    )
    b2 = builder.build(
        user_text="帮我跑一下测试",
        user_content="帮我跑一下测试",
        core_facts=facts,
        rolling_summary="",
        recent_messages=[],
        recalled_memories=[],
        context_store=ContextStore(),
        recalled_skills=[
            MemorySkill(
                skill_id="skill_1",
                name="运行 pytest",
                trigger_intent="测试",
                steps_json='["运行 pytest -v"]',
            )
        ],
    )
    # Skill injection happens in L3 dynamic tail, keeping static L0+L1 prefix byte-identical
    assert b1.stats.static_prefix_hash == b2.stats.static_prefix_hash
    assert b1.messages[0]["content"] == b2.messages[0]["content"]
    assert "skill_1" in b2.messages[-1]["content"]


async def test_tool_executor_multi_step_and_max_steps_cap() -> None:
    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="m")},
    )
    t_search = DummyTool("search_mock", risk="safe")
    t_echo = DummyTool("echo_mock", risk="safe")
    reg = ToolRegistry([t_search, t_echo])

    router = ScriptedToolRouter(
        cfg,
        [
            LLMCompletionResult(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="search_mock", arguments_json='{"x": "1"}')],
                usage=LLMUsage(prompt_tokens=100, prompt_cache_hit_tokens=80, completion_tokens=20, total_tokens=120),
            ),
            LLMCompletionResult(
                content="",
                tool_calls=[ToolCallRequest(id="c2", name="echo_mock", arguments_json='{"x": "2"}')],
                usage=LLMUsage(prompt_tokens=150, prompt_cache_hit_tokens=120, completion_tokens=25, total_tokens=175),
            ),
            LLMCompletionResult(
                content="[EMOTION:happy]\n两步工具调用都已完成！",
                usage=LLMUsage(prompt_tokens=200, prompt_cache_hit_tokens=180, completion_tokens=30, total_tokens=230),
            ),
        ],
    )
    executor = ToolExecutor(router)
    ctx = ToolExecutionContext(turn_id="t1", session_id="s1", config=cfg)
    streamed: List[str] = []

    async def on_delta(tok: str) -> None:
        streamed.append(tok)

    res = await executor.run(
        messages=[{"role": "user", "content": "查并回显"}],
        registry=reg,
        ctx=ctx,
        on_text_delta=on_delta,
        max_steps=5,
    )
    assert res.stopped_reason == "completed"
    assert res.called_tools == ["search_mock", "echo_mock"]
    assert len(res.trace_steps) == 2
    assert "两步工具调用都已完成" in "".join(streamed)

    # Verify max_steps cap stops runaway loops cleanly
    loop_router = ScriptedToolRouter(
        cfg,
        [
            LLMCompletionResult(
                tool_calls=[ToolCallRequest(id=f"c_{i}", name="search_mock", arguments_json='{"x":"loop"}')]
            )
            for i in range(4)
        ]
        + [LLMCompletionResult(content="已按步数上限收尾总结。")],
    )
    res2 = await ToolExecutor(loop_router).run(
        messages=[{"role": "user", "content": "loop"}],
        registry=reg,
        ctx=ToolExecutionContext(turn_id="t2", session_id="s1", config=cfg),
        max_steps=2,
    )
    assert res2.stopped_reason == "max_steps"
    assert len(res2.trace_steps) == 2


async def test_tool_executor_circuit_breaker_on_consecutive_errors() -> None:
    cfg = AppConfig()
    broken = DummyTool("flaky_tool", risk="safe", fail=True)
    reg = ToolRegistry([broken])
    router = ScriptedToolRouter(
        cfg,
        [
            LLMCompletionResult(tool_calls=[ToolCallRequest(id="e1", name="flaky_tool", arguments_json="{}")]),
            LLMCompletionResult(tool_calls=[ToolCallRequest(id="e2", name="flaky_tool", arguments_json="{}")]),
            LLMCompletionResult(tool_calls=[ToolCallRequest(id="e3", name="flaky_tool", arguments_json="{}")]),
            LLMCompletionResult(content="工具连续报错两次，已止损停用。"),
        ],
    )
    res = await ToolExecutor(router).run(
        messages=[{"role": "user", "content": "run"}],
        registry=reg,
        ctx=ToolExecutionContext(turn_id="t3", session_id="s1", config=cfg),
        max_steps=6,
    )
    assert res.stopped_reason == "circuit_breaker"
    assert len(broken.calls) == 2


def test_hub_approval_allow_deny_remember_and_turn_cancel() -> None:
    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="m")},
    )
    cfg.memory.sqlite_path = ":memory:"
    cfg.memory.extraction.enabled = False

    confirm_tool = DummyTool("confirm_op", risk="confirm")
    reg = ToolRegistry([confirm_tool])

    # Router emits confirm_op twice across turns so we can test deny -> allow+remember -> auto-allow -> cancel
    script = [
        LLMCompletionResult(tool_calls=[ToolCallRequest(id="a1", name="confirm_op", arguments_json='{"x":"first"}')]),
        LLMCompletionResult(content="[EMOTION:neutral]\n好的，已跳过该操作。"),
        LLMCompletionResult(tool_calls=[ToolCallRequest(id="a2", name="confirm_op", arguments_json='{"x":"second"}')]),
        LLMCompletionResult(content="[EMOTION:happy]\n已允许并完成。"),
        LLMCompletionResult(tool_calls=[ToolCallRequest(id="a3", name="confirm_op", arguments_json='{"x":"third"}')]),
        LLMCompletionResult(content="[EMOTION:happy]\n记住授权后直接完成。"),
        LLMCompletionResult(tool_calls=[ToolCallRequest(id="a4", name="confirm_op", arguments_json='{"x":"fourth"}')]),
    ]
    router = ScriptedToolRouter(cfg, script)
    app = create_app(config=cfg, llm_router=router)
    app.state.hub.tool_registry = reg

    def _recv_until(ws, target_type: str) -> Dict[str, Any]:
        while True:
            msg = json.loads(ws.receive_text())
            if msg["type"] == target_type:
                return msg

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "h1",
                        "ts": 1,
                        "source": "body",
                        "type": "system.hello",
                        "payload": {"client_name": "test", "role": "body"},
                    }
                )
            )
            _recv_until(ws, "system.welcome")

            # Turn 1: Deny approval
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "m1",
                        "turn_id": "turn_deny",
                        "ts": 2,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "执行 confirm 1"},
                    }
                )
            )
            apr1 = _recv_until(ws, "tool.approval_request")
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "r1",
                        "turn_id": "turn_deny",
                        "ts": 3,
                        "source": "body",
                        "type": "tool.approval_response",
                        "payload": {
                            "approval_id": apr1["payload"]["approval_id"],
                            "turn_id": "turn_deny",
                            "approved": False,
                            "remember_for_session": False,
                        },
                    }
                )
            )
            _recv_until(ws, "output.turn_end")
            assert len(confirm_tool.calls) == 0

            # Turn 2: Allow & remember for session
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "m2",
                        "turn_id": "turn_allow",
                        "ts": 4,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "执行 confirm 2"},
                    }
                )
            )
            apr2 = _recv_until(ws, "tool.approval_request")
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "r2",
                        "turn_id": "turn_allow",
                        "ts": 5,
                        "source": "body",
                        "type": "tool.approval_response",
                        "payload": {
                            "approval_id": apr2["payload"]["approval_id"],
                            "turn_id": "turn_allow",
                            "approved": True,
                            "remember_for_session": True,
                        },
                    }
                )
            )
            _recv_until(ws, "output.turn_end")
            assert len(confirm_tool.calls) == 1

            # Turn 3: Should skip approval because remember_for_session=True
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "m3",
                        "turn_id": "turn_mem",
                        "ts": 6,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "执行 confirm 3"},
                    }
                )
            )
            _recv_until(ws, "output.turn_end")
            assert len(confirm_tool.calls) == 2

            # Turn 4: Cancel mid-approval using kill_switch when session approval cleared
            app.state.hub._session_approved_tools.clear()
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "m4",
                        "turn_id": "turn_kill",
                        "ts": 7,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "执行 confirm 4"},
                    }
                )
            )
            _recv_until(ws, "tool.approval_request")
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "k1",
                        "turn_id": "turn_kill",
                        "ts": 8,
                        "source": "body",
                        "type": "input.cancel",
                        "payload": {"reason": "kill_switch"},
                    }
                )
            )
            ended = _recv_until(ws, "output.turn_end")
            assert ended["payload"]["finish_reason"] == "cancelled"
            assert len(confirm_tool.calls) == 2
