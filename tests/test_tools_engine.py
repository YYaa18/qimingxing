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


async def test_tool_executor_passes_reasoning_and_server_tools_back() -> None:
    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="m")},
    )
    reg = ToolRegistry([DummyTool("search_mock", risk="safe")])
    router = ScriptedToolRouter(
        cfg,
        [
            LLMCompletionResult(
                content="",
                reasoning_content="先查一下",
                server_tool_calls=[
                    {"index": 0, "calls": [{"index": 0, "id": "srv_1", "type": "deepseek_web_search"}]}
                ],
                tool_calls=[ToolCallRequest(id="c1", name="search_mock", arguments_json='{"x": "1"}')],
            ),
            LLMCompletionResult(content="完成"),
        ],
    )
    executor = ToolExecutor(router)
    ctx = ToolExecutionContext(turn_id="t1", session_id="s1", config=cfg)

    await executor.run(
        messages=[{"role": "user", "content": "查"}],
        registry=reg,
        ctx=ctx,
    )

    assert len(router.recorded_messages) >= 2
    assistant = next(
        m for m in router.recorded_messages[1] if m.get("role") == "assistant"
    )
    assert assistant["reasoning_content"] == "先查一下"
    assert assistant["tool_calls"][0]["id"] == "c1"
    assert assistant["server_tool_calls"][0]["calls"][0]["id"] == "srv_1"


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
    app.state.hub.router_extra_tool_names = {"confirm_op"}

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
            app.state.hub._approved_tools_by_client.clear()
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


async def test_p4_01_shell_remember_for_session_is_scoped_and_blocks_mutating(tmp_path) -> None:
    from brain.config import ShellToolConfig
    from brain.tools.shell_tool import ShellTool

    shell = ShellTool(ShellToolConfig(default_cwd=str(tmp_path)))

    # Read-only / benign commands get a narrow command-prefix scope key
    assert shell.approval_scope_key({"command": "python3 --version"}) == "shell_exec:python3 --version"
    assert shell.format_approval_details({"command": "python3 --version"})["allow_remember"] is True

    # Mutating / chained / arbitrary code commands cannot be cached in session
    assert shell.approval_scope_key({"command": "rm -f tmp.txt"}) is None
    assert shell.approval_scope_key({"command": "echo hi > a.txt"}) is None
    assert shell.approval_scope_key({"command": "git push origin main"}) is None
    assert shell.approval_scope_key({"command": "python3 -c 'print(1)'"}) is None
    assert shell.format_approval_details({"command": "rm -f tmp.txt"})["allow_remember"] is False

    # Verify ToolExecutor enforces scoped cache: approving python3 --version does NOT auto-approve other commands
    cfg = AppConfig()
    cfg.tools.shell = ShellToolConfig(default_cwd=str(tmp_path))
    reg = ToolRegistry([shell])
    router = ScriptedToolRouter(
        cfg,
        [
            LLMCompletionResult(
                tool_calls=[ToolCallRequest(id="s1", name="shell_exec", arguments_json='{"command":"python3 --version"}')]
            ),
            LLMCompletionResult(
                tool_calls=[ToolCallRequest(id="s2", name="shell_exec", arguments_json='{"command":"python3 --version"}')]
            ),
            LLMCompletionResult(
                tool_calls=[ToolCallRequest(id="s3", name="shell_exec", arguments_json='{"command":"echo hi > out.txt"}')]
            ),
            LLMCompletionResult(
                tool_calls=[ToolCallRequest(id="s4", name="shell_exec", arguments_json='{"command":"echo hi > out.txt"}')]
            ),
            LLMCompletionResult(content="done"),
        ],
    )
    approval_prompts: List[str] = []

    async def fake_approval(tool_name, risk_level, summary, details, timeout_sec):
        approval_prompts.append(str(details.get("command")))
        # Client even tries sending remember=True for every prompt
        return True, True

    ctx = ToolExecutionContext(
        turn_id="t_scope",
        session_id="s1",
        config=cfg,
        on_approval=fake_approval,
    )
    res = await ToolExecutor(router).run(
        messages=[{"role": "user", "content": "test scoped approval"}],
        registry=reg,
        ctx=ctx,
        max_steps=8,
    )
    assert res.stopped_reason == "completed"
    # 1 prompt for python3 --version (2nd skipped due to narrow scope match),
    # then BOTH echo hi > out.txt calls must prompt even if client claimed remember=True!
    assert approval_prompts == [
        "python3 --version",
        "echo hi > out.txt",
        "echo hi > out.txt",
    ]
    assert (tmp_path / "out.txt").exists()


async def test_circuit_breaker_replies_to_every_tool_call_id() -> None:
    """A provider rejects the wrap-up request if an assistant tool_calls
    message is missing tool replies for any declared id."""
    cfg = AppConfig()
    broken = DummyTool("flaky_tool", risk="safe", fail=True)
    reg = ToolRegistry([broken])
    router = ScriptedToolRouter(
        cfg,
        [
            LLMCompletionResult(
                tool_calls=[
                    ToolCallRequest(id="c1", name="flaky_tool", arguments_json="{}"),
                    ToolCallRequest(id="c2", name="flaky_tool", arguments_json="{}"),
                    ToolCallRequest(id="c3", name="flaky_tool", arguments_json="{}"),
                ]
            ),
            LLMCompletionResult(content="工具连续报错，已止损。"),
        ],
    )
    res = await ToolExecutor(router).run(
        messages=[{"role": "user", "content": "run"}],
        registry=reg,
        ctx=ToolExecutionContext(turn_id="t4", session_id="s1", config=cfg),
        max_steps=6,
    )
    assert res.stopped_reason == "circuit_breaker"
    assert len(broken.calls) == 2

    wrap_messages = router.recorded_messages[-1]
    assistant_ids = {
        tc["id"]
        for m in wrap_messages
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
    }
    tool_ids = {m.get("tool_call_id") for m in wrap_messages if m.get("role") == "tool"}
    assert assistant_ids == {"c1", "c2", "c3"}
    assert assistant_ids <= tool_ids
    assert any("SKIPPED" in str(m.get("content", "")) for m in wrap_messages if m.get("role") == "tool")


async def test_mandate_covers_doc_write_without_second_approval(tmp_path: Path) -> None:
    cfg = AppConfig()
    doc_tool = DummyTool("doc_generate", risk="confirm")
    reg = ToolRegistry([doc_tool])
    router = ScriptedToolRouter(
        cfg,
        [
            LLMCompletionResult(
                tool_calls=[ToolCallRequest(id="d1", name="doc_generate", arguments_json='{"x": "1"}')]
            ),
            LLMCompletionResult(content="已生成。"),
        ],
    )
    from brain.worker import WorkerMandate

    ctx = ToolExecutionContext(
        turn_id="t5",
        session_id="s1",
        config=cfg,
        worker_mandate=WorkerMandate(
            origin="explicit_user",
            allowed_packs=["doc_office"],
            writable_paths=[str(tmp_path)],
        ),
    )
    res = await ToolExecutor(router).run(
        messages=[{"role": "user", "content": "生成"}],
        registry=reg,
        ctx=ctx,
    )
    assert res.stopped_reason == "completed"
    assert len(doc_tool.calls) == 1


def test_compact_dispatch_goal_keeps_business_paragraph() -> None:
    from brain.tools.orchestration_tools import compact_dispatch_goal

    goal = (
        "在目录 /tmp/invoices 下把行程单按 Excel 序号重命名一份副本，并核对缺 PDF 的项目；原件只读。\n"
        "步骤要求：\n"
        "1. 先枚举目录内所有 xlsx 和 pdf。\n"
        "2. 调用 doc_match_rename，参数 table_path=... output_dir=...\n"
        "3. 生成核对报告。"
    )
    compact = compact_dispatch_goal(goal)
    assert compact.startswith("在目录 /tmp/invoices")
    assert "doc_match_rename" not in compact
    assert "步骤要求" not in compact

    plain = "帮我查一下今天的汇率并给出结论。"
    assert compact_dispatch_goal(plain) == plain


async def test_executor_recovers_dsml_leaked_only_in_stream_text() -> None:
    """Some providers stream DSML as plain text after a tool round. The
    executor must recover it and run the tool, never return markup."""
    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="m")},
    )
    snap = DummyTool("browser_snapshot", risk="safe")
    reg = ToolRegistry([snap])
    leaked = (
        '<｜｜DSML｜｜ calls>\n'
        '<｜｜DSML｜｜ invoke name="browser_snapshot">\n'
        '<｜｜DSML｜｜ parameter name="max_elements" string="false">100</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n'
        '</｜｜DSML｜｜ calls>'
    )
    router = ScriptedToolRouter(
        cfg,
        [
            LLMCompletionResult(content=leaked),
            LLMCompletionResult(content="已读取 100 个元素。"),
        ],
    )
    result = await ToolExecutor(router).run(
        messages=[{"role": "user", "content": "看看网页"}],
        registry=reg,
        ctx=ToolExecutionContext(turn_id="dsml1", session_id="s", config=cfg),
    )
    assert result.stopped_reason == "completed"
    assert snap.calls == [{"max_elements": 100}]
    assert "DSML" not in result.final_text
    assert result.final_text == "已读取 100 个元素。"


async def test_bg_wrap_up_never_returns_raw_dsml() -> None:
    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="m")},
    )
    snap = DummyTool("browser_snapshot", risk="safe")
    reg = ToolRegistry([snap])
    leaked = (
        '<｜｜DSML｜｜ calls>\n'
        '<｜｜DSML｜｜ invoke name="browser_snapshot">\n'
        '<｜｜DSML｜｜ parameter name="max_elements" string="false">100</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n'
        '</｜｜DSML｜｜ calls>'
    )
    router = ScriptedToolRouter(
        cfg,
        [
            LLMCompletionResult(content=leaked),
            LLMCompletionResult(content=leaked),
            LLMCompletionResult(content="已收尾，这是最终清单。"),
        ],
    )
    result = await ToolExecutor(router).run(
        messages=[{"role": "user", "content": "看看网页"}],
        registry=reg,
        ctx=ToolExecutionContext(turn_id="dsml2", session_id="s", config=cfg),
        max_steps=1,
    )
    assert result.stopped_reason == "max_steps"
    assert "DSML" not in result.final_text
    assert result.final_text == "已收尾，这是最终清单。"


async def test_bg_wrap_up_persistent_dsml_returns_friendly_fallback() -> None:
    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="m")},
    )
    snap = DummyTool("browser_snapshot", risk="safe")
    reg = ToolRegistry([snap])
    leaked = (
        '<｜｜DSML｜｜ calls>\n'
        '<｜｜DSML｜｜ invoke name="browser_snapshot">\n'
        '<｜｜DSML｜｜ parameter name="max_elements" string="false">100</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n'
        '</｜｜DSML｜｜ calls>'
    )
    router = ScriptedToolRouter(
        cfg,
        [
            LLMCompletionResult(content=leaked),
            LLMCompletionResult(content=leaked),
            LLMCompletionResult(content=leaked),
        ],
    )
    result = await ToolExecutor(router).run(
        messages=[{"role": "user", "content": "看看网页"}],
        registry=reg,
        ctx=ToolExecutionContext(turn_id="dsml3", session_id="s", config=cfg),
        max_steps=1,
    )
    assert "DSML" not in result.final_text
    assert "连续返回无法解析" in result.final_text
