from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from brain.config import AppConfig, ShellToolConfig, WebSearchToolConfig
from brain.tools.base import ToolExecutionContext
from brain.tools.shell_tool import ShellTool
from brain.tools.web_search import DeepSeekSearchProvider, WebFetchTool, WebSearchTool


async def test_deepseek_search_and_fetch_with_mock_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tools/search"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Python 3.12 Release Notes",
                            "url": "https://docs.python.org/3.12/whatsnew/3.12.html",
                            "site_name": "python.org",
                            "date": "2026-11-22T00:00:00Z",
                            "summary": "Key features of Python 3.12 plus secret sk-verysecretkey123456",
                        }
                    ]
                },
            )
        if request.url.path.endswith("/tools/fetch"):
            long_md = "HEAD_PART " + ("X" * 500) + " TAIL_PART"
            return httpx.Response(200, json={"title": "Python 3.12 Docs", "content": long_md})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    ws_cfg = WebSearchToolConfig(api_key="sk-verysecretkey123456", max_fetch_chars=180)
    app_cfg = AppConfig()
    app_cfg.tools.web_search = ws_cfg

    provider = DeepSeekSearchProvider(ws_cfg, transport=transport)
    search_tool = WebSearchTool(ws_cfg, provider=provider)
    fetch_tool = WebFetchTool(ws_cfg, provider=provider)
    ctx = ToolExecutionContext(turn_id="t_web", session_id="s1", config=app_cfg)

    s_res = await search_tool.execute({"query": "Python 3.12"}, ctx)
    assert s_res.ok is True
    assert "Python 3.12 Release Notes" in s_res.output
    assert "sk-verysecretkey123456" not in s_res.output

    f_res = await fetch_tool.execute(
        {"url": "https://docs.python.org/3.12/whatsnew/3.12.html"}, ctx
    )
    assert f_res.ok is True
    assert "HEAD_PART" in f_res.output
    assert "TAIL_PART" in f_res.output
    assert "截断" in f_res.output


def test_shell_tool_three_tier_risk_classification() -> None:
    shell = ShellTool(ShellToolConfig())

    assert shell.classify_risk({"command": "pwd"}) == "safe"
    assert shell.classify_risk({"command": "ls -la"}) == "safe"
    assert shell.classify_risk({"command": "git status"}) == "safe"

    # Writes, piping, chaining, scripts & external CLIs must be CONFIRM
    assert shell.classify_risk({"command": "python3 --version"}) == "confirm"
    assert shell.classify_risk({"command": "pytest -v"}) == "confirm"
    assert shell.classify_risk({"command": "ls && pwd"}) == "confirm"
    assert shell.classify_risk({"command": "echo hi > tmp.txt"}) == "confirm"
    assert shell.classify_risk({"command": "cat a.txt | wc -l"}) == "confirm"

    # Destructive or secret-exfiltrating patterns must be BLOCKED
    assert shell.classify_risk({"command": "rm -rf /"}) == "blocked"
    assert shell.classify_risk({"command": "sudo ls /root"}) == "blocked"
    assert shell.classify_risk({"command": "cat config.yaml"}) == "blocked"
    assert shell.classify_risk({"command": "cat .env"}) == "blocked"
    assert shell.classify_risk({"command": "printenv"}) == "blocked"
    assert shell.classify_risk({"command": "cat ~/.ssh/id_rsa"}) == "blocked"
    assert shell.classify_risk({"command": "git status && rm -rf /"}) == "blocked"


async def test_shell_tool_execution_timeout_and_output_truncation(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.tools.shell = ShellToolConfig(timeout_sec=1, default_cwd=str(tmp_path), max_output_chars=240)
    shell = ShellTool(cfg.tools.shell)
    ctx = ToolExecutionContext(turn_id="t_sh", session_id="s1", config=cfg)

    # Normal execution in tmp_path
    res_ok = await shell.execute({"command": "pwd && python3 -c 'print(\"hello_shell\")'"}, ctx)
    assert res_ok.ok is True
    assert "hello_shell" in res_ok.output

    # Timeout kills child process promptly
    res_to = await shell.execute({"command": "sleep 5"}, ctx)
    assert res_to.ok is False
    assert "超时" in res_to.output

    # Large output triggers head+tail truncation
    res_big = await shell.execute(
        {"command": "python3 -c 'print(\"HEAD_MARKER_\" + \"A\"*600 + \"_TAIL_MARKER\")'"},
        ctx,
    )
    assert res_big.ok is True
    assert "HEAD_MARKER_" in res_big.output
    assert "_TAIL_MARKER" in res_big.output
    assert "截断" in res_big.output

    # Cancellation cleanly aborts subprocess
    task = asyncio.create_task(shell.execute({"command": "sleep 10"}, ctx))
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
        assert False, "Expected CancelledError"
    except asyncio.CancelledError:
        pass
