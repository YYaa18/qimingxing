from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Sequence, Tuple

from brain.config import AppConfig
from brain.tools.base import ToolExecutionContext
from brain.tools.macos_apps import (
    MacOSBrowserTabsTool,
    MacOSCalendarCreateTool,
    MacOSCalendarListTool,
    MacOSMailDraftTool,
    MacOSMailSearchTool,
    MacOSNotesCreateTool,
    MacOSNotesSearchTool,
    MacOSRemindersCreateTool,
    MacOSRemindersListTool,
    run_osascript,
)


def _ctx(cfg: AppConfig | None = None) -> ToolExecutionContext:
    return ToolExecutionContext(turn_id="t_mac", session_id="s_mac", config=cfg or AppConfig())


async def test_run_osascript_argv_isolation_and_timeout() -> None:
    captured: list[tuple[str, list[str]]] = []

    async def _mock_runner(script: str, argv: Sequence[str], timeout: float) -> Tuple[int, str, str]:
        captured.append((script, list(argv)))
        return 0, json.dumps([{"name": "Note1", "body": "ok"}]), ""

    malicious = '"; rm -rf /; $(cat /etc/passwd) `whoami` \\"'
    tool = MacOSNotesSearchTool(runner=_mock_runner)
    res = await tool.execute({"query": malicious}, _ctx())
    assert res.ok
    assert len(captured) == 1
    script, argv = captured[0]
    assert malicious not in script
    assert argv[0] == malicious

    # Real timeout guard using sleep in subprocess_exec path via a slow mock command or python
    rc, _, err = await run_osascript("while(true){}", [], timeout_sec=0.05)
    # On macOS osascript times out (124); on non-macOS FileNotFoundError (127)
    assert rc in (124, 127)
    assert err != ""


async def test_macos_automation_1743_permission_guidance() -> None:
    async def _denied_runner(script: str, argv: Sequence[str], timeout: float) -> Tuple[int, str, str]:
        return 1, "", "execution error: Not authorized to send Apple events to Notes. (-1743)"

    tool = MacOSNotesCreateTool(runner=_denied_runner)
    assert tool.classify_risk({"title": "t", "body": "b"}) == "confirm"
    res = await tool.execute({"title": "测试", "body": "内容"}, _ctx())
    assert not res.ok
    assert "-1743" in res.output
    assert "系统设置" in res.output
    assert "自动化" in res.output


async def test_notes_calendar_reminders_risk_and_mock_execution() -> None:
    async def _ok_runner(script: str, argv: Sequence[str], timeout: float) -> Tuple[int, str, str]:
        if "Calendar" in script and "events.push" not in script:
            return 0, json.dumps([{"calendar": "工作", "summary": "周会", "start": "2026-11-26T10:00:00Z", "end": "2026-11-26T11:00:00Z", "location": ""}]), ""
        if "Reminders" in script and "Reminder(props)" not in script and "completed = true" not in script:
            return 0, json.dumps([{"list": "默认", "name": "买牛奶", "due": "", "body": ""}]), ""
        return 0, json.dumps({"ok": True}), ""

    notes_create = MacOSNotesCreateTool(runner=_ok_runner)
    cal_list = MacOSCalendarListTool(runner=_ok_runner)
    cal_create = MacOSCalendarCreateTool(runner=_ok_runner)
    rem_list = MacOSRemindersListTool(runner=_ok_runner)
    rem_create = MacOSRemindersCreateTool(runner=_ok_runner)

    assert cal_list.classify_risk({}) == "safe"
    assert rem_list.classify_risk({}) == "safe"
    assert notes_create.classify_risk({}) == "confirm"
    assert cal_create.classify_risk({}) == "confirm"
    assert rem_create.classify_risk({}) == "confirm"

    details = cal_create.format_approval_details(
        {"title": "评审会", "start_iso": "2026-11-26T15:00:00+08:00", "calendar_name": "工作"}
    )
    assert details["title"] == "评审会"
    assert details["calendar_name"] == "工作"

    r1 = await cal_list.execute({"days": 7}, _ctx())
    r2 = await rem_list.execute({}, _ctx())
    r3 = await notes_create.execute({"title": "日报", "body": "已完成"}, _ctx())
    r4 = await cal_create.execute({"title": "评审会", "start_iso": "2026-11-26T15:00:00+08:00"}, _ctx())
    r5 = await rem_create.execute({"action": "create", "title": "提交代码"}, _ctx())
    assert all(r.ok for r in (r1, r2, r3, r4, r5))
    assert "周会" in r1.output
    assert "买牛奶" in r2.output


async def test_browser_tabs_privacy_blacklist_filtering() -> None:
    fake_tabs = [
        {"browser": "Safari", "title": "FastAPI Docs", "url": "https://fastapi.tiangolo.com/"},
        {"browser": "Chrome", "title": "招商银行个人网银", "url": "https://pbsz.ebank.cmbchina.com/"},
        {"browser": "Arc", "title": "Bitwarden Web Vault", "url": "https://vault.bitwarden.com/"},
        {"browser": "Safari", "title": "无痕浏览 - 隐私页面", "url": "https://example.com/private"},
        {"browser": "Chrome", "title": "支付宝 - 登录", "url": "https://auth.alipay.com/"},
    ]

    async def _tabs_runner(script: str, argv: Sequence[str], timeout: float) -> Tuple[int, str, str]:
        return 0, json.dumps(fake_tabs), ""

    tool = MacOSBrowserTabsTool(runner=_tabs_runner)
    assert tool.classify_risk({}) == "safe"
    res = await tool.execute({"browser": "all"}, _ctx())
    assert res.ok
    assert "FastAPI Docs" in res.output
    assert "cmbchina" not in res.output
    assert "bitwarden" not in res.output.lower()
    assert "alipay" not in res.output.lower()
    assert "无痕" not in res.output
    assert res.metadata["count"] == 1
    assert res.metadata["filtered_count"] == 4


async def test_mail_search_redacted_and_mail_draft_never_sends() -> None:
    cfg = AppConfig()
    cfg._secrets.append("sk-mail-leak-secret-5566")

    async def _mail_runner(script: str, argv: Sequence[str], timeout: float) -> Tuple[int, str, str]:
        if "inbox.messages" in script:
            return 0, json.dumps([
                {
                    "subject": "部署密钥",
                    "sender": "ops@example.com",
                    "date": "2026-11-25T10:00:00Z",
                    "snippet": "Token is sk-mail-leak-secret-5566 " + ("A" * 5000),
                }
            ]), ""
        return 0, json.dumps({"ok": True, "draft_created": True}), ""

    search_tool = MacOSMailSearchTool(runner=_mail_runner)
    draft_tool = MacOSMailDraftTool(runner=_mail_runner)
    assert search_tool.classify_risk({}) == "safe"
    assert draft_tool.classify_risk({}) == "confirm"

    sr = await search_tool.execute({"query": "部署"}, _ctx(cfg))
    assert sr.ok
    assert len(sr.output) <= 4000
    assert "sk-mail-leak-secret-5566" not in sr.output
    assert "***REDACTED***" in sr.output

    dr = await draft_tool.execute({"to": "a@example.com", "subject": "周报", "body": "正文"}, _ctx(cfg))
    assert dr.ok
    assert "草稿" in dr.output

    # Static verification: zero send-mail calls in macos_apps.py
    src = Path(__file__).resolve().parent.parent.joinpath("brain/tools/macos_apps.py").read_text(encoding="utf-8")
    assert ".send(" not in src
    assert "send()" not in src
