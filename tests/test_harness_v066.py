from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import yaml
from fastapi.testclient import TestClient

from brain.config import AppConfig, ProfileConfig
from brain.context_builder import SOURCE_ATTRIBUTION_REMINDER, ContextBuilder, get_grounding_guidelines
from brain.context_store import ContextStore
from brain.eval_runner import EvalReport, format_eval_report, run_golden_evals, score_subchecks
from brain.llm_router import LLMCompletionResult, LLMRouter, LLMUsage, ToolCallRequest
from brain.main import create_app
from brain.memory import cli as memory_cli
from brain.tools import AskUserTool, ToolExecutionContext, build_default_tool_registry


class _ScriptedAskUserRouter(LLMRouter):
    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self._step = 0
        self.tool_outputs: List[str] = []

    async def complete_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        profile_name: Optional[str] = None,
        on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> LLMCompletionResult:
        self._step += 1
        if self._step == 1:
            return LLMCompletionResult(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call_ask_1",
                        name="ask_user",
                        arguments_json=json.dumps(
                            {
                                "question": "要怎么整理这些文件？",
                                "options": ["按项目归档", "合并为摘要"],
                            },
                            ensure_ascii=False,
                        ),
                    )
                ],
                usage=LLMUsage(prompt_tokens=20, completion_tokens=15, total_tokens=35),
                finish_reason="tool_calls",
            )
        tool_msgs = [str(m.get("content", "")) for m in messages if m.get("role") == "tool"]
        self.tool_outputs = tool_msgs
        reply = f"[EMOTION:happy]\n收到！已按你的选择执行（根据澄清结果：{tool_msgs[-1]}）。"
        if on_text_delta is not None:
            await on_text_delta(reply)
        return LLMCompletionResult(
            content=reply,
            tool_calls=[],
            usage=LLMUsage(prompt_tokens=30, completion_tokens=20, total_tokens=50),
            finish_reason="stop",
        )


async def test_ask_user_tool_answered_and_denied() -> None:
    cfg = AppConfig()
    tool = AskUserTool()
    assert tool.classify_risk({}) == "safe"
    assert tool.approval_scope_key({}) is None

    async def _reply(q: str, opts: List[str], _t: int) -> Optional[str]:
        assert "整理" in q
        assert opts == ["A", "B"]
        return "按 A 处理"

    ctx_ok = ToolExecutionContext(turn_id="t1", session_id="s1", config=cfg, on_ask_user=_reply)
    res_ok = await tool.execute({"question": "怎么整理？", "options": ["A", "B"]}, ctx_ok)
    assert res_ok.ok is True
    assert "按 A 处理" in res_ok.output

    async def _cancel(_q: str, _opts: List[str], _t: int) -> Optional[str]:
        return None

    ctx_no = ToolExecutionContext(turn_id="t2", session_id="s1", config=cfg, on_ask_user=_cancel)
    res_no = await tool.execute({"question": "怎么整理？"}, ctx_no)
    assert res_no.ok is False
    assert res_no.status == "denied"
    assert "请勿盲目猜测" in res_no.output


def test_ask_user_hub_websocket_roundtrip(tmp_path: Path) -> None:
    cfg = AppConfig(
        default_profile="mock_local",
        profiles={
            "mock_local": ProfileConfig(
                name="mock_local",
                label="Mock",
                provider="mock",
                model="mock",
            )
        },
    )
    cfg.memory.sqlite_path = str(tmp_path / "ask_user_hub.db")
    cfg.memory.extraction.enabled = False
    cfg.audio.enabled = False
    cfg.tools.enabled = True

    reg = build_default_tool_registry(cfg)
    assert reg.get("ask_user") is not None

    router = _ScriptedAskUserRouter(cfg)
    app = create_app(config=cfg, llm_router=router)

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
                        "payload": {"client_name": "body_godot", "role": "body"},
                    }
                )
            )
            while json.loads(ws.receive_text())["type"] != "system.privacy_state":
                pass

            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "m1",
                        "turn_id": "turn_ask_1",
                        "ts": 2,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "帮我整理一下这些文件"},
                    }
                )
            )

            spoken: List[str] = []
            clarify_seen = False
            while True:
                msg = json.loads(ws.receive_text())
                if msg["type"] == "tool.approval_request":
                    p = msg["payload"]
                    assert p["tool_name"] == "ask_user"
                    assert p["risk_level"] == "safe"
                    assert p["details"]["kind"] == "clarification"
                    assert p["details"]["options"] == ["按项目归档", "合并为摘要"]
                    clarify_seen = True
                    ws.send_text(
                        json.dumps(
                            {
                                "v": "1.0",
                                "id": "r1",
                                "turn_id": "turn_ask_1",
                                "ts": 3,
                                "source": "body",
                                "type": "tool.approval_response",
                                "payload": {
                                    "approval_id": p["approval_id"],
                                    "turn_id": "turn_ask_1",
                                    "approved": True,
                                    "remember_for_session": False,
                                    "answer_text": "合并为摘要",
                                },
                            }
                        )
                    )
                elif msg["type"] == "output.text_delta":
                    spoken.append(msg["payload"]["delta"])
                elif msg["type"] == "output.turn_end":
                    break

            assert clarify_seen is True
            joined = "".join(spoken)
            assert "合并为摘要" in joined
            assert any("合并为摘要" in t for t in router.tool_outputs)


def test_source_attribution_and_grounding_guidelines() -> None:
    guidelines = get_grounding_guidelines()
    assert "结论来源分层标注" in guidelines
    assert "以下为推测/建议" in guidelines
    assert "明确拒答" in guidelines

    builder = ContextBuilder()
    without_extra = builder.build(
        user_text="早安",
        user_content="早安",
        core_facts=[],
        rolling_summary="",
        recent_messages=[],
        recalled_memories=[],
        context_store=ContextStore(),
    )
    assert SOURCE_ATTRIBUTION_REMINDER not in str(without_extra.messages[-1]["content"])

    with_extra = builder.build(
        user_text="帮我看下文件",
        user_content="帮我看下文件",
        core_facts=[],
        rolling_summary="",
        recent_messages=[],
        recalled_memories=[],
        context_store=ContextStore(),
        extra_l3_sections=["【批量路径清单】\n- report.md"],
    )
    assert SOURCE_ATTRIBUTION_REMINDER in str(with_extra.messages[-1]["content"])


async def test_golden_evals_and_na_separation(tmp_path: Path, capsys: Any) -> None:
    # 1. Official golden cases >= 10, all passing
    official_report = await run_golden_evals()
    assert official_report.total >= 10
    assert official_report.failed == 0
    assert official_report.partial == 0
    assert official_report.not_applicable == 0
    assert official_report.score_pct == 100.0
    rendered = format_eval_report(official_report)
    assert "100.0%" in rendered

    # 2. Graded 1.0 / 0.5 / 0.0 and strict separation of not_applicable (None vs 0.0)
    st, sc, _ = score_subchecks([("a", True), ("b", False)])
    assert st == "partial" and sc == 0.5

    custom_yaml = tmp_path / "custom_eval.yaml"
    custom_yaml.write_text(
        yaml.safe_dump(
            {
                "cases": [
                    {"id": "c_pass", "category": "sec", "title": "pass", "checker": "shell_risk_classify", "command": "git status", "expected_risk": "safe"},
                    {"id": "c_fail", "category": "sec", "title": "fail", "checker": "shell_risk_classify", "command": "rm -rf /", "expected_risk": "safe"},
                    {"id": "c_na", "category": "voice", "title": "mic missing", "checker": "privacy_meta_only_refusal", "applicable": False, "skip_reason": "无麦克风设备"},
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    mixed_report: EvalReport = await run_golden_evals(custom_yaml)
    assert mixed_report.total == 3
    assert mixed_report.applicable == 2
    assert mixed_report.passed == 1
    assert mixed_report.failed == 1
    assert mixed_report.not_applicable == 1
    assert mixed_report.score_pct == 50.0  # (1.0 + 0.0) / 2, excluding N/A!


def test_golden_eval_cli_command(capsys: Any) -> None:
    rc = memory_cli.main(["eval", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["total"] >= 10
    assert out["failed"] == 0
