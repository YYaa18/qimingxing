from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import yaml
from fastapi.testclient import TestClient

from brain.config import AppConfig, MCPServerConfig, ProfileConfig, load_config
from brain.llm_router import LLMCompletionResult, LLMRouter, ToolCallRequest
from brain.main import create_app
from brain.memory.cli import main as cli_main
from brain.tools import ToolExecutionContext, ToolRegistry, build_default_tool_registry
from brain.tools.mcp_client import MCPClientManager


@dataclass
class _FakeToolSchema:
    name: str
    description: str
    inputSchema: Dict[str, Any]


@dataclass
class _FakeTextContent:
    text: str


@dataclass
class _FakeCallResult:
    content: List[_FakeTextContent]
    isError: bool = False


class _FakeMCPSession:
    def __init__(self, server_name: str, secret_to_leak: str = "") -> None:
        self.server_name = server_name
        self.secret_to_leak = secret_to_leak
        self.calls: List[tuple[str, Dict[str, Any]]] = []

    async def list_tools(self) -> Any:
        return type(
            "ListToolsRes",
            (),
            {
                "tools": [
                    _FakeToolSchema(
                        name="write_file",
                        description="Write text to a file",
                        inputSchema={
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["path", "content"],
                        },
                    ),
                    _FakeToolSchema(
                        name="read_file",
                        description="Read text from a file",
                        inputSchema={
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    ),
                ]
            },
        )()

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        args = dict(arguments or {})
        self.calls.append((name, args))
        if name == "read_file":
            return _FakeCallResult(
                content=[_FakeTextContent(text=f"file content with {self.secret_to_leak}")]
            )
        return _FakeCallResult(content=[_FakeTextContent(text=f"wrote {args.get('path')}")])


class _FakeScriptedMCPRouter(LLMRouter):
    def __init__(self, config: AppConfig, steps: List[LLMCompletionResult]) -> None:
        super().__init__(config)
        self.steps = list(steps)

    async def complete_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        profile_name: Optional[str] = None,
        on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> LLMCompletionResult:
        res = self.steps.pop(0) if self.steps else LLMCompletionResult(content="done")
        if res.content and on_text_delta is not None:
            await on_text_delta(res.content)
        return res


async def test_mcp_manager_fault_isolation_whitelist_sorting_and_cleanup() -> None:
    cfg = AppConfig(
        mcp_servers=[
            MCPServerConfig(
                name="fs",
                enabled=True,
                transport="stdio",
                command="npx",
                safe_tools=["read_file"],
            ),
            MCPServerConfig(
                name="broken_srv",
                enabled=True,
                transport="stdio",
                command="/nonexistent/binary",
            ),
            MCPServerConfig(
                name="disabled_srv",
                enabled=False,
                transport="sse",
                url="http://127.0.0.1:9999/sse",
            ),
        ]
    )
    cfg._secrets.append("sk-mcpsecret-token-888")

    fake_sess = _FakeMCPSession("fs", secret_to_leak="sk-mcpsecret-token-888")
    closed_stacks: List[str] = []

    async def opener(srv: MCPServerConfig, stack):
        if srv.name == "broken_srv":
            raise FileNotFoundError("binary missing")
        stack.callback(lambda s=srv.name: closed_stacks.append(s))
        return fake_sess

    reg = build_default_tool_registry(cfg)
    mgr = MCPClientManager(cfg, transport_opener=opener)
    statuses = await mgr.start_all(reg)

    status_map = {s.name: s for s in statuses}
    assert status_map["fs"].status == "connected"
    assert status_map["broken_srv"].status == "offline"
    assert "FileNotFoundError" in status_map["broken_srv"].error
    assert status_map["disabled_srv"].status == "disabled"

    # Deterministic sorting in ToolRegistry
    names = [s["function"]["name"] for s in reg.to_openai_tools()]
    assert names == sorted(names)
    assert "mcp__fs__read_file" in names
    assert "mcp__fs__write_file" in names

    # Whitelisted safe tool -> SAFE; non-whitelisted -> CONFIRM + approval_scope_key=None
    t_read = reg.get("mcp__fs__read_file")
    t_write = reg.get("mcp__fs__write_file")
    assert t_read is not None and t_read.classify_risk({}) == "safe"
    assert t_write is not None and t_write.classify_risk({}) == "confirm"
    assert t_write.approval_scope_key({}) is None
    assert t_write.format_approval_details({"path": "a.txt"})["allow_remember"] is False

    # Teardown unregisters MCP tools and closes AsyncExitStack cleanly
    await mgr.stop_all(reg)
    assert reg.get("mcp__fs__read_file") is None
    assert closed_stacks == ["fs"]


def test_mcp_hub_e2e_safe_and_confirm_approval_with_secret_redaction(tmp_path: Path) -> None:
    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="m")},
        mcp_servers=[
            MCPServerConfig(
                name="fs",
                enabled=True,
                transport="streamable_http",
                url="http://127.0.0.1:8123/mcp",
                headers={"Authorization": "Bearer tok-mcp-header-secret"},
                safe_tools=["read_file"],
            )
        ],
    )
    cfg.memory.sqlite_path = str(tmp_path / "mcp_e2e.db")
    cfg.memory.extraction.enabled = False

    fake_sess = _FakeMCPSession("fs", secret_to_leak="tok-mcp-header-secret")

    async def opener(srv: MCPServerConfig, stack):
        return fake_sess

    mgr = MCPClientManager(cfg, transport_opener=opener)
    router = _FakeScriptedMCPRouter(
        cfg,
        [
            # Turn 1: safe read_file runs silently
            LLMCompletionResult(
                tool_calls=[
                    ToolCallRequest(
                        id="m1", name="mcp__fs__read_file", arguments_json='{"path":"notes.txt"}'
                    )
                ]
            ),
            LLMCompletionResult(content="[EMOTION:happy]\n读取完了，隐私 token 已脱敏。"),
            # Turn 2: non-whitelisted write_file triggers CONFIRM -> user denies
            LLMCompletionResult(
                tool_calls=[
                    ToolCallRequest(
                        id="m2",
                        name="mcp__fs__write_file",
                        arguments_json='{"path":"out.txt","content":"abc"}',
                    )
                ]
            ),
            LLMCompletionResult(content="用户拒绝了写入，未修改文件。"),
            # Turn 3: non-whitelisted write_file -> user allows
            LLMCompletionResult(
                tool_calls=[
                    ToolCallRequest(
                        id="m3",
                        name="mcp__fs__write_file",
                        arguments_json='{"path":"out.txt","content":"abc"}',
                    )
                ]
            ),
            LLMCompletionResult(content="[EMOTION:happy]\n文件已写入！"),
        ],
    )

    app = create_app(config=cfg, llm_router=router, mcp_manager=mgr)
    # MCP tools are Worker-only in production (Router prefix isolation).
    # This e2e test explicitly opts them into the Router surface.
    app.state.hub.router_include_mcp = True

    def _recv_until(ws, t: str) -> Dict[str, Any]:
        while True:
            msg = json.loads(ws.receive_text())
            if msg["type"] == t:
                return msg

    with TestClient(app) as client:
        dbg = client.get("/api/memory/debug").json()
        assert dbg["mcp_servers"][0]["status"] == "connected"

        with client.websocket_connect("/ws") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "h",
                        "ts": 1,
                        "source": "body",
                        "type": "system.hello",
                        "payload": {"client_name": "body", "role": "body"},
                    }
                )
            )
            _recv_until(ws, "system.welcome")

            # Turn 1: SAFE -> no approval prompt
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "q1",
                        "turn_id": "t1",
                        "ts": 2,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "读文件"},
                    }
                )
            )
            _recv_until(ws, "output.turn_end")
            assert len(fake_sess.calls) == 1

            # Turn 2: CONFIRM -> Deny
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "q2",
                        "turn_id": "t2",
                        "ts": 3,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "写文件1"},
                    }
                )
            )
            apr1 = _recv_until(ws, "tool.approval_request")
            assert apr1["payload"]["details"]["allow_remember"] is False
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "r1",
                        "turn_id": "t2",
                        "ts": 4,
                        "source": "body",
                        "type": "tool.approval_response",
                        "payload": {
                            "approval_id": apr1["payload"]["approval_id"],
                            "turn_id": "t2",
                            "approved": False,
                        },
                    }
                )
            )
            _recv_until(ws, "output.turn_end")
            assert len(fake_sess.calls) == 1  # Not written

            # Turn 3: CONFIRM -> Allow
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "q3",
                        "turn_id": "t3",
                        "ts": 5,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "写文件2"},
                    }
                )
            )
            apr2 = _recv_until(ws, "tool.approval_request")
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "r2",
                        "turn_id": "t3",
                        "ts": 6,
                        "source": "body",
                        "type": "tool.approval_response",
                        "payload": {
                            "approval_id": apr2["payload"]["approval_id"],
                            "turn_id": "t3",
                            "approved": True,
                        },
                    }
                )
            )
            _recv_until(ws, "output.turn_end")
            assert len(fake_sess.calls) == 2


def test_mcp_config_parsing_and_cli(tmp_path: Path, capsys) -> None:
    cfg_path = tmp_path / "mcp_cfg.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "default_profile": "mock_local",
                "profiles": {
                    "mock_local": {"label": "Mock", "provider": "mock", "model": "m"}
                },
                "mcp_servers": [
                    {
                        "name": "fs",
                        "enabled": True,
                        "transport": "stdio",
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                        "safe_tools": ["read_text_file"],
                    },
                    {
                        "name": "remote_http",
                        "enabled": False,
                        "transport": "streamable_http",
                        "url": "https://example.com/mcp",
                        "headers": {"X-Api-Key": "tok-mcp-secret-999"},
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    loaded = load_config(cfg_path)
    assert len(loaded.mcp_servers) == 2
    assert "tok-mcp-secret-999" in loaded.collect_secrets()

    db_path = tmp_path / "cli_mcp.db"
    assert cli_main(["--config", str(cfg_path), "--db", str(db_path), "mcp"]) == 0
    out = capsys.readouterr().out
    assert "fs | stdio | enabled" in out
    assert "remote_http | streamable_http | disabled" in out


def test_p5_01_mcp_auto_reconnect_retry_and_http_reload(tmp_path: Path) -> None:
    cfg_a = AppConfig(
        mcp_servers=[
            MCPServerConfig(
                name="flaky",
                enabled=True,
                transport="stdio",
                command="echo",
                safe_tools=["ping"],
            )
        ]
    )

    class _PingSession:
        def __init__(self, *, fail_with: Optional[Exception] = None) -> None:
            self.fail_with = fail_with
            self.calls = 0

        async def list_tools(self) -> Any:
            return type(
                "ListToolsRes",
                (),
                {
                    "tools": [
                        _FakeToolSchema(
                            name="ping",
                            description="Ping server",
                            inputSchema={"type": "object", "properties": {}},
                        )
                    ]
                },
            )()

        async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
            self.calls += 1
            if self.fail_with is not None:
                raise self.fail_with
            return _FakeCallResult(content=[_FakeTextContent(text="recovered_ok")])

    async def _exercise_reconnect() -> None:
        open_calls = 0
        closed_stacks = 0

        async def opener(srv: MCPServerConfig, stack):
            nonlocal open_calls, closed_stacks
            open_calls += 1

            def _on_close() -> None:
                nonlocal closed_stacks
                closed_stacks += 1

            stack.callback(_on_close)
            if open_calls == 1:
                return _PingSession(fail_with=ConnectionResetError("broken pipe"))
            return _PingSession(fail_with=None)

        reg = build_default_tool_registry(cfg_a)
        mgr = MCPClientManager(cfg_a, transport_opener=opener)
        await mgr.start_all(reg)
        assert open_calls == 1

        tool = reg.get("mcp__flaky__ping")
        assert tool is not None
        ctx = ToolExecutionContext(turn_id="t-p501", session_id="s-p501", config=cfg_a)
        res = await tool.execute({}, ctx)
        assert res.ok is True
        assert "recovered_ok" in res.output
        assert open_calls == 2
        assert closed_stacks == 1
        assert mgr.list_statuses()[0].status == "connected"
        await mgr.stop_all(reg)

    asyncio.run(_exercise_reconnect())

    cfg_b = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="m")},
        mcp_servers=[
            MCPServerConfig(
                name="flaky",
                enabled=True,
                transport="stdio",
                command="echo",
                safe_tools=["ping"],
            )
        ],
    )
    cfg_b.memory.sqlite_path = str(tmp_path / "mcp_reload.db")
    cfg_b.memory.extraction.enabled = False

    reload_open_calls = 0

    async def reload_opener(srv: MCPServerConfig, stack):
        nonlocal reload_open_calls
        reload_open_calls += 1
        return _PingSession(fail_with=None)

    mgr_b = MCPClientManager(cfg_b, transport_opener=reload_opener)
    app = create_app(config=cfg_b, mcp_manager=mgr_b)
    with TestClient(app) as client:
        assert reload_open_calls == 1
        resp = client.post("/api/mcp/reload")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["mcp_servers"][0]["name"] == "flaky"
        assert body["mcp_servers"][0]["status"] == "connected"
        assert reload_open_calls == 2


async def test_p5_02_mcp_output_4000_char_truncation_and_secret_redaction() -> None:
    cfg = AppConfig(
        profiles={
            "p": ProfileConfig(
                name="p",
                label="P",
                provider="openai_compat",
                model="m",
                api_key="cfg-secret-key-xyz999",
            )
        },
        mcp_servers=[
            MCPServerConfig(
                name="logger",
                enabled=True,
                transport="stdio",
                command="echo",
                safe_tools=["read_logs"],
            )
        ],
    )

    payload_text = (
        "HEAD_MARKER auth=Bearer tok_1234567890 key=cfg-secret-key-xyz999\n"
        + ("L" * 6000)
        + "\ntoken=sk-abcdefgh12345678 TAIL_MARKER"
    )

    class _LargeOutputSession:
        async def list_tools(self) -> Any:
            return type(
                "ListToolsRes",
                (),
                {
                    "tools": [
                        _FakeToolSchema(
                            name="read_logs",
                            description="Read huge logs",
                            inputSchema={"type": "object", "properties": {}},
                        )
                    ]
                },
            )()

        async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
            return _FakeCallResult(content=[_FakeTextContent(text=payload_text)])

    async def opener(srv: MCPServerConfig, stack):
        return _LargeOutputSession()

    reg = build_default_tool_registry(cfg)
    mgr = MCPClientManager(cfg, transport_opener=opener)
    await mgr.start_all(reg)

    tool = reg.get("mcp__logger__read_logs")
    assert tool is not None
    ctx = ToolExecutionContext(turn_id="t-p502", session_id="s-p502", config=cfg)
    res = await tool.execute({}, ctx)
    await mgr.stop_all(reg)

    assert res.ok is True
    assert len(res.output) <= 4100
    assert "截断" in res.output
    assert "HEAD_MARKER" in res.output
    assert "TAIL_MARKER" in res.output
    assert "***REDACTED***" in res.output
    assert "cfg-secret-key-xyz999" not in res.output
    assert "tok_1234567890" not in res.output
    assert "sk-abcdefgh12345678" not in res.output
