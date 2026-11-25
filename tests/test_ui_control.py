from __future__ import annotations

import pytest

from brain.config import AppConfig
from brain.tools import build_default_tool_registry
from brain.tools.base import ToolExecutionContext
from brain.tools.ui_control import OpenQimingxingUITool


def _ctx(sender=None) -> ToolExecutionContext:
    return ToolExecutionContext(
        turn_id="t_ui",
        session_id="s_ui",
        config=AppConfig(),
        on_ui_command=sender,
    )


@pytest.mark.asyncio
async def test_open_workspace_dispatches_safe_ui_command():
    calls = []

    async def sender(action, args):
        calls.append((action, args))
        return True, ""

    tool = OpenQimingxingUITool()
    assert tool.classify_risk({"action": "open_workspace"}) == "safe"
    result = await tool.execute({"action": "open_workspace"}, _ctx(sender))

    assert result.ok is True
    assert calls == [("open_workspace", {})]
    assert "工作区" in result.output


@pytest.mark.asyncio
async def test_open_settings_passes_tab():
    calls = []

    async def sender(action, args):
        calls.append((action, args))
        return True, ""

    result = await tool_execute({"action": "open_settings", "tab": "routing"}, sender)
    assert result.ok is True
    assert calls == [("open_settings", {"tab": "routing"})]


@pytest.mark.asyncio
async def test_ui_tool_reports_offline_shell():
    async def sender(action, args):
        return False, "shell_offline"

    result = await tool_execute({"action": "open_workspace"}, sender)
    assert result.ok is False
    assert result.status == "error"
    assert "shell_offline" in result.output


@pytest.mark.asyncio
async def test_ui_tool_without_bridge_is_unavailable():
    result = await tool_execute({"action": "open_workspace"}, None)
    assert result.ok is False
    assert "ui_unavailable" in result.output


@pytest.mark.asyncio
async def test_ui_tool_rejects_unknown_action():
    result = await tool_execute({"action": "launch_rockets"}, None)
    assert result.ok is False
    assert result.status == "error"


def test_registry_includes_qimingxing_ui_tool():
    registry = build_default_tool_registry(AppConfig())
    assert registry.get("open_qimingxing_ui") is not None


async def tool_execute(arguments, sender):
    tool = OpenQimingxingUITool()
    return await tool.execute(arguments, _ctx(sender))


def test_ui_tool_command_reaches_native_shell(tmp_path) -> None:
    import json

    from fastapi.testclient import TestClient

    from brain.config import ProfileConfig
    from brain.llm_router import LLMCompletionResult, LLMRouter, ToolCallRequest
    from brain.main import create_app

    class _Router(LLMRouter):
        async def complete_with_tools(
            self, messages, tools, *, profile_name=None, on_text_delta=None
        ):
            if any(m.get("role") == "tool" for m in messages):
                return LLMCompletionResult(content="工作区已打开。")
            return LLMCompletionResult(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="tc_ui",
                        name="open_qimingxing_ui",
                        arguments_json='{"action":"open_workspace"}',
                    )
                ],
            )

    cfg = AppConfig(
        default_profile="mock_local",
        profiles={
            "mock_local": ProfileConfig(
                name="mock_local", label="Mock", provider="mock", model="mock-v1"
            )
        },
    )
    cfg.memory.sqlite_path = str(tmp_path / "ui_tool.db")
    cfg.memory.extraction.enabled = False
    app = create_app(config=cfg, llm_router=_Router(cfg))

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as shell_ws:
            shell_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "h_ui",
                        "ts": 1,
                        "source": "body",
                        "type": "system.hello",
                        "payload": {
                            "client_name": "qimingxing-macos",
                            "role": "body",
                            "client_type": "body_macos",
                            "platform": "macos",
                            "app_version": "1.0.0",
                            "capabilities": [
                                "mirror_turns",
                                "native_approvals",
                                "native_desktop",
                                "audio_output",
                            ],
                        },
                    }
                )
            )
            for _ in range(3):
                shell_ws.receive_text()

            shell_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "m_ui",
                        "turn_id": "t_ui_open",
                        "ts": 2,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "打开我的工作区"},
                    }
                )
            )

            saw_command = False
            saw_user_text = False
            for _ in range(40):
                event = json.loads(shell_ws.receive_text())
                if event["type"] == "output.turn_start":
                    assert event["payload"]["user_text"] == "打开我的工作区"
                    saw_user_text = True
                if event["type"] == "ui.command":
                    payload = event["payload"]
                    assert payload["action"] == "open_workspace"
                    assert payload["args"] == {}
                    shell_ws.send_text(
                        json.dumps(
                            {
                                "v": "1.0",
                                "id": "r_ui",
                                "ts": 3,
                                "source": "body",
                                "type": "ui.command_result",
                                "payload": {
                                    "command_id": payload["command_id"],
                                    "ok": True,
                                    "error": "",
                                    "data": {},
                                },
                            }
                        )
                    )
                    saw_command = True
                if event["type"] == "output.turn_end":
                    break

            assert saw_command, "open_workspace never reached the native shell"
            assert saw_user_text, "turn_start did not mirror the user prompt"
