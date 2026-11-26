from __future__ import annotations

import ast
import json
import os
import time
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from brain.config import AppConfig, ProfileConfig
from brain.main import create_app, is_allowed_ws_origin
from brain.memory import create_memory_backend
from brain.memory.backend import SNAPSHOT_SCHEMA_VERSION
from brain.memory.models import BackgroundTaskRecord
from brain.tools.base import Tool, ToolExecutionContext, ToolResult
from protocol.models import RiskLevel


class _SharedApprovalTool(Tool):
    name = "shared_approval_tool"
    description = "Tool to test dual-client approval sync & reconnect recovery."
    parameters_schema = {"type": "object", "properties": {}}
    default_risk: RiskLevel = "confirm"

    def classify_risk(self, arguments: dict) -> RiskLevel:
        return "confirm"

    async def execute(self, arguments: dict, ctx: ToolExecutionContext) -> ToolResult:
        return ToolResult(ok=True, output="ok", summary="ok")


def test_ws_origin_guard_rejects_evil_origins(tmp_path: Path) -> None:
    assert is_allowed_ws_origin("", 18790)
    assert is_allowed_ws_origin("http://127.0.0.1:18790", 18790)
    assert is_allowed_ws_origin("http://localhost:18790", 18790)
    assert not is_allowed_ws_origin("https://evil.example.com", 18790)
    assert not is_allowed_ws_origin("http://127.0.0.1:9999", 18790)

    cfg = AppConfig()
    cfg.memory.sqlite_path = str(tmp_path / "ws_origin.db")
    cfg.memory.extraction.enabled = False
    app = create_app(config=cfg)

    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ws", headers={"Origin": "https://evil.example.com"}):
                pass
        with client.websocket_connect("/ws", headers={"Origin": "http://127.0.0.1:18790"}) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "h1",
                        "ts": 1,
                        "source": "body",
                        "type": "system.hello",
                        "payload": {"client_name": "body_web", "role": "body"},
                    }
                )
            )
            welcome = json.loads(ws.receive_text())
            assert welcome["type"] == "system.welcome"
            assert welcome["payload"]["server_version"] == "1.0.1"


def test_ui_state_ui_command_and_approval_reconnect_sync(tmp_path: Path) -> None:
    from brain.llm_router import LLMCompletionResult, LLMRouter, ToolCallRequest

    class _ToolCallingRouter(LLMRouter):
        async def complete_with_tools(self, messages, tools, *, profile_name=None, on_text_delta=None):
            if any(m.get("role") == "tool" for m in messages):
                return LLMCompletionResult(content="已完成操作。")
            return LLMCompletionResult(
                content="",
                tool_calls=[ToolCallRequest(id="tc1", name="shared_approval_tool", arguments_json="{}")],
            )

    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="mock-v1")},
    )
    cfg.memory.sqlite_path = str(tmp_path / "ui_sync.db")
    cfg.memory.extraction.enabled = False
    app = create_app(config=cfg, llm_router=_ToolCallingRouter(cfg))
    app.state.hub.tool_registry.register(_SharedApprovalTool())
    app.state.hub.router_extra_tool_names = {"shared_approval_tool"}

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as shell_ws:
            shell_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "h_mac",
                        "ts": 1,
                        "source": "body",
                        "type": "system.hello",
                        "payload": {
                            "client_name": "qimingxing-macos",
                            "role": "body",
                            "client_type": "body_macos",
                            "platform": "macos",
                            "app_version": "1.0.0",
                            "capabilities": ["mirror_turns", "native_approvals", "native_desktop", "audio_output"],
                        },
                    }
                )
            )
            assert json.loads(shell_ws.receive_text())["type"] == "system.welcome"
            assert json.loads(shell_ws.receive_text())["type"] == "system.privacy_state"
            ui_state = json.loads(shell_ws.receive_text())
            assert ui_state["type"] == "ui.state"
            assert ui_state["payload"]["avatar_enabled"] is False

            # ui.set_state updates and broadcasts ui.state
            shell_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "u1",
                        "ts": 2,
                        "source": "body",
                        "type": "ui.set_state",
                        "payload": {"origin": "shell_macos", "patch": {"earcons": False}},
                    }
                )
            )
            ui_updated = json.loads(shell_ws.receive_text())
            assert ui_updated["type"] == "ui.state"
            assert ui_updated["payload"]["earcons"] is False

            # Dangerous open_path extension blocked by Brain
            shell_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "c1",
                        "ts": 3,
                        "source": "body",
                        "type": "ui.command",
                        "payload": {
                            "command_id": "cmd_bad",
                            "target": "shell",
                            "action": "open_path",
                            "args": {"path": "/tmp/evil.sh"},
                        },
                    }
                )
            )
            cmd_res = json.loads(shell_ws.receive_text())
            assert cmd_res["type"] == "ui.command_result"
            assert cmd_res["payload"]["ok"] is False
            assert cmd_res["payload"]["error"] == "dangerous_executable_blocked"

            # A harmless-looking symlink must be resolved before extension
            # checks: link.md -> Evil.app still counts as a dangerous .app.
            evil_app = tmp_path / "Evil.app"
            evil_app.mkdir()
            link_path = tmp_path / "innocent.md"
            os.symlink(evil_app, link_path)
            shell_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "c_symlink",
                        "ts": 3,
                        "source": "body",
                        "type": "ui.command",
                        "payload": {
                            "command_id": "cmd_symlink",
                            "target": "shell",
                            "action": "open_path",
                            "args": {"path": str(link_path)},
                        },
                    }
                )
            )
            symlink_res = json.loads(shell_ws.receive_text())
            assert symlink_res["type"] == "ui.command_result"
            assert symlink_res["payload"]["error"] == "dangerous_executable_blocked"
            assert symlink_res["payload"]["data"]["suffix"] == ".app"

            # Explicit body_macos priority even when a Godot body exists first.
            hub = app.state.hub
            original_clients = hub.clients
            hub.clients = {
                "godot": SimpleNamespace(
                    role="body",
                    client_type="body_godot",
                    capabilities=SimpleNamespace(audio_output=True, native_desktop=False),
                ),
                "macos": SimpleNamespace(
                    role="body",
                    client_type="body_macos",
                    capabilities=SimpleNamespace(audio_output=False, native_desktop=True),
                ),
            }
            assert hub._get_primary_body_conn() is hub.clients["macos"]
            hub.clients = original_clients

            # Offline avatar target returns target_offline
            shell_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "c2",
                        "ts": 4,
                        "source": "body",
                        "type": "ui.command",
                        "payload": {
                            "command_id": "cmd_off",
                            "target": "avatar",
                            "action": "toggle_dock",
                            "args": {},
                        },
                    }
                )
            )
            off_res = json.loads(shell_ws.receive_text())
            assert off_res["type"] == "ui.command_result"
            assert off_res["payload"]["error"] == "target_offline"

            # Trigger approval on shell_ws
            shell_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "m1",
                        "turn_id": "t_apr",
                        "ts": 5,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "执行审批工具"},
                    }
                )
            )
            apr_req = None
            for _ in range(10):
                msg = json.loads(shell_ws.receive_text())
                if msg["type"] == "tool.approval_request":
                    apr_req = msg
                    break
            assert apr_req is not None

            # Connect a second client (simulating reconnect / web UI) and confirm pending_approvals recovery
            with client.websocket_connect("/ws") as web_ws:
                web_ws.send_text(
                    json.dumps(
                        {
                            "v": "1.0",
                            "id": "h_web",
                            "ts": 6,
                            "source": "body",
                            "type": "system.hello",
                            "payload": {"client_name": "body_web", "role": "body"},
                        }
                    )
                )
                assert json.loads(web_ws.receive_text())["type"] == "system.welcome"
                assert json.loads(web_ws.receive_text())["type"] == "system.privacy_state"
                recovered_ui = json.loads(web_ws.receive_text())
                assert recovered_ui["type"] == "ui.state"
                assert len(recovered_ui["payload"]["pending_approvals"]) == 1
                aid = recovered_ui["payload"]["pending_approvals"][0]["approval_id"]

                # Resolve from web_ws -> shell_ws receives tool.approval_resolved
                web_ws.send_text(
                    json.dumps(
                        {
                            "v": "1.0",
                            "id": "resp1",
                            "turn_id": "t_apr",
                            "ts": 7,
                            "source": "body",
                            "type": "tool.approval_response",
                            "payload": {
                                "approval_id": aid,
                                "turn_id": "t_apr",
                                "approved": True,
                                "remember_for_session": False,
                            },
                        }
                    )
                )

                resolved_seen = False
                for _ in range(15):
                    ev = json.loads(shell_ws.receive_text())
                    if ev["type"] == "tool.approval_resolved":
                        resolved_seen = True
                        assert ev["payload"]["approval_id"] == aid
                        assert ev["payload"]["approved"] is True
                        break
                assert resolved_seen


async def test_sqlite_discipline_and_jsonl_6_0_migration(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parent.parent
    importers: list[str] = []
    for folder in ("brain", "protocol", "senses"):
        for pyf in (root / folder).rglob("*.py"):
            tree = ast.parse(pyf.read_text(encoding="utf-8"), filename=str(pyf))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(a.name == "sqlite3" for a in node.names):
                    importers.append(str(pyf.relative_to(root)))
                elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
                    importers.append(str(pyf.relative_to(root)))
    assert sorted(importers) == [
        "brain/memory/sqlite_store.py",
        "brain/model_config_store.py",
    ]
    assert SNAPSHOT_SCHEMA_VERSION == "6.0"

    cfg1 = AppConfig()
    cfg1.memory.sqlite_path = str(tmp_path / "src.db")
    b1 = create_memory_backend(cfg1.memory)
    await b1.initialize()
    snap = tmp_path / "snap_v6.jsonl"
    await b1.add_background_task(
        BackgroundTaskRecord(
            task_id="bg_mig_1",
            title="迁移验证",
            goal_prompt="测试",
            status="completed",
            progress_pct=100,
            result_summary="全部通过",
            artifact_path="/tmp/bg_mig_1.md",
        )
    )
    exported = await b1.export_snapshot(snap)
    await b1.close()

    cfg2 = AppConfig()
    cfg2.memory.sqlite_path = str(tmp_path / "dst.db")
    b2 = create_memory_backend(cfg2.memory)
    await b2.initialize()
    imported = await b2.import_snapshot(snap)
    rec = await b2.get_background_task("bg_mig_1")
    await b2.close()

    assert exported == imported
    assert rec is not None
    assert rec.title == "迁移验证"
    assert rec.result_summary == "全部通过"


def test_foreground_approval_survives_same_instance_reconnect(tmp_path: Path) -> None:
    from brain.llm_router import LLMCompletionResult, LLMRouter, ToolCallRequest

    class _ReconnectRouter(LLMRouter):
        async def complete_with_tools(self, messages, tools, *, profile_name=None, on_text_delta=None):
            if any(m.get("role") == "tool" for m in messages):
                return LLMCompletionResult(content="审批后完成。")
            return LLMCompletionResult(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="tc_reconnect",
                        name="shared_approval_tool",
                        arguments_json="{}",
                    )
                ],
            )

    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="mock-v1")},
    )
    cfg.memory.sqlite_path = str(tmp_path / "reconnect.db")
    cfg.memory.extraction.enabled = False
    app = create_app(config=cfg, llm_router=_ReconnectRouter(cfg))
    app.state.hub.tool_registry.register(_SharedApprovalTool())
    app.state.hub.router_extra_tool_names = {"shared_approval_tool"}

    def _hello(ws) -> None:
        ws.send_text(
            json.dumps(
                {
                    "v": "1.0",
                    "id": "h_reconnect",
                    "ts": 1,
                    "source": "body",
                    "type": "system.hello",
                    "payload": {
                        "client_name": "qimingxing-macos",
                        "role": "body",
                        "client_type": "body_macos",
                        "platform": "macos",
                        "client_instance_id": "shell-instance-1",
                        "capabilities": ["mirror_turns", "native_approvals", "native_desktop", "audio_output"],
                    },
                }
            )
        )

    with TestClient(app) as client:
        approval_id = ""
        with client.websocket_connect("/ws") as ws:
            _hello(ws)
            for _ in range(3):
                ws.receive_text()
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "m_reconnect",
                        "turn_id": "t_reconnect",
                        "ts": 2,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "执行审批工具"},
                    }
                )
            )
            for _ in range(30):
                ev = json.loads(ws.receive_text())
                if ev["type"] == "tool.approval_request":
                    approval_id = ev["payload"]["approval_id"]
                    break

        assert approval_id, "approval request was not emitted before disconnect"
        time.sleep(0.5)

        with client.websocket_connect("/ws") as ws2:
            _hello(ws2)
            assert json.loads(ws2.receive_text())["type"] == "system.welcome"
            assert json.loads(ws2.receive_text())["type"] == "system.privacy_state"
            ui = json.loads(ws2.receive_text())
            assert ui["type"] == "ui.state"
            pending = ui["payload"]["pending_approvals"]
            assert any(item["approval_id"] == approval_id for item in pending), pending

            ws2.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "resp_reconnect",
                        "turn_id": "t_reconnect",
                        "ts": 3,
                        "source": "body",
                        "type": "tool.approval_response",
                        "payload": {
                            "approval_id": approval_id,
                            "turn_id": "t_reconnect",
                            "approved": True,
                            "remember_for_session": False,
                        },
                    }
                )
            )

            seen_resolved = False
            seen_end = False
            for _ in range(40):
                ev = json.loads(ws2.receive_text())
                if ev["type"] == "tool.approval_resolved":
                    seen_resolved = True
                if ev["type"] == "output.turn_end" and ev.get("turn_id") == "t_reconnect":
                    seen_end = True
                if seen_resolved and seen_end:
                    break

            assert seen_resolved
            assert seen_end
