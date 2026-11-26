from __future__ import annotations

import asyncio
import functools
import http.server
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from brain.browser_runtime import (
    BrowserRuntime,
    domain_allowed,
    validate_browser_url,
)
from brain.config import AppConfig
from brain.tools import build_default_tool_registry
from brain.tools.base import ToolExecutionContext
from brain.tools.browser_tools import BrowserActTool, BrowserOpenTool
from brain.tools.orchestration_tools import TaskDispatchTool
from brain.worker import (
    WORKER_PACK_TOOLS,
    WorkerMandate,
    build_router_registry,
    build_worker_registry,
)

PAGE_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>测试页</title></head><body>
<h1>查询</h1>
<input id="q" aria-label="关键词">
<button id="go" onclick="document.getElementById('out').innerText='结果:'+document.getElementById('q').value">查询</button>
<div id="out"></div>
<button id="submit" onclick="document.getElementById('out').innerText='已提交'">提交订单</button>
<table><tr><th>城市</th><th>金额</th></tr><tr><td>北京</td><td>100</td></tr></table>
</body></html>"""


@pytest.fixture()
def local_site(tmp_path: Path):
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text(PAGE_HTML, encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/index.html"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _chrome_available() -> bool:
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return False
    return Path("/Applications/Google Chrome.app").exists()


requires_chrome = pytest.mark.skipif(
    not _chrome_available(), reason="Playwright/Chrome not installed"
)


def test_brw01_router_never_exposes_browser_tools() -> None:
    cfg = AppConfig()
    full = build_default_tool_registry(cfg)
    router_names = {tool.name for tool in build_router_registry(full).list_tools()}
    assert not {name for name in router_names if name.startswith("browser_")}
    pack = WORKER_PACK_TOOLS["browser_auto"]
    assert pack == {
        "browser_open",
        "browser_snapshot",
        "browser_act",
        "browser_extract",
        "browser_handoff",
    }
    worker_names = {tool.name for tool in build_worker_registry(full, ["browser_auto"]).list_tools()}
    assert pack <= worker_names


def test_brw03_url_scheme_and_ssrf_gate() -> None:
    for bad in (
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,hi",
        "chrome://settings",
        "http://169.254.169.254/latest/meta-data",
        "http://127.0.0.1:8000/",
        "http://localhost:8000/",
        "http://192.168.1.10/",
    ):
        with pytest.raises(ValueError):
            validate_browser_url(bad)
    assert validate_browser_url("https://example.com/a") == "https://example.com/a"
    assert domain_allowed("sub.example.com", ["example.com"])
    assert not domain_allowed("other.local", ["allowed.local"])
    with pytest.raises(ValueError):
        validate_browser_url("https://other.local/", allowed_domains=["allowed.local"])


def test_brw05_mandate_lock_keys_and_serialization(tmp_path: Path) -> None:
    isolated = WorkerMandate(allowed_packs=["browser_auto"], browser_mode="isolated")
    live = WorkerMandate(
        allowed_packs=["browser_auto"],
        browser_mode="live",
        allowed_domains=["allowed.local"],
    )
    assert "browser:isolated_profile" in isolated.resource_keys()
    assert "browser:live_session" in live.resource_keys()

    from brain.memory import create_memory_backend

    cfg = AppConfig()
    cfg.memory.sqlite_path = str(tmp_path / "locks.db")

    async def run() -> None:
        backend = create_memory_backend(cfg.memory)
        await backend.initialize()
        try:
            assert await backend.acquire_resource_lock(
                "browser:isolated_profile", "task_a", mode="exclusive", lease_seconds=60
            )
            assert not await backend.acquire_resource_lock(
                "browser:isolated_profile", "task_b", mode="exclusive", lease_seconds=60
            )
            await backend.release_resource_lock("browser:isolated_profile", "task_a")
            assert await backend.acquire_resource_lock(
                "browser:isolated_profile", "task_b", mode="exclusive", lease_seconds=60
            )
            await backend.release_resource_lock("browser:isolated_profile", "task_b")
        finally:
            await backend.close()

    asyncio.run(run())


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def submit(self, **kwargs: Any):
        self.calls.append(kwargs)
        return SimpleNamespace(task_id="bg_test", title=kwargs.get("title", ""))


def test_brw06_dispatch_requires_domains_for_live_and_records_mandate() -> None:
    cfg = AppConfig()
    tool = TaskDispatchTool()
    runner = _FakeRunner()
    ctx = ToolExecutionContext(turn_id="t", session_id="s", config=cfg, task_runner=runner)

    bad = asyncio.run(
        tool.execute(
            {
                "goal": "打开后台并读取数据",
                "packs": ["browser_auto"],
                "browser_mode": "live",
            },
            ctx,
        )
    )
    assert not bad.ok
    assert not runner.calls

    ok = asyncio.run(
        tool.execute(
            {
                "goal": "打开后台并读取数据",
                "packs": ["browser_auto"],
                "browser_mode": "live",
                "allowed_domains": ["allowed.local"],
            },
            ctx,
        )
    )
    assert ok.ok
    mandate = runner.calls[0]["mandate"]
    assert mandate["browser_mode"] == "live"
    assert mandate["allowed_domains"] == ["allowed.local"]
    assert runner.calls[0]["packs"] == ["browser_auto"]


def test_browser_open_rejects_live_escalation() -> None:
    cfg = AppConfig()
    ctx = ToolExecutionContext(
        turn_id="t",
        session_id="s",
        config=cfg,
        worker_mandate=WorkerMandate(
            allowed_packs=["browser_auto"],
            browser_mode="isolated",
            allowed_domains=["allowed.local"],
        ),
    )
    res = asyncio.run(
        BrowserOpenTool().execute({"url": "https://allowed.local/", "mode": "live"}, ctx)
    )
    assert not res.ok
    assert res.status == "blocked"
    assert "live" in res.output


@requires_chrome
def test_brw02_isolated_snapshot_act_extract(local_site: str, tmp_path: Path) -> None:
    async def run() -> None:
        runtime = BrowserRuntime(
            turn_id="brw02",
            profile_dir=tmp_path / "profile",
            media_dir=tmp_path / "media",
            downloads_dir=tmp_path / "downloads",
            allow_private=True,
            headless=True,
        )
        try:
            state = await runtime.open(local_site)
            assert state["title"] == "测试页"
            snap = await runtime.snapshot(30)
            refs = {el["name"]: el["ref"] for el in snap["elements"]}
            assert refs["关键词"]
            assert refs["查询"]
            assert "提交订单" in refs
            await runtime.act(action="fill", ref_id=refs["关键词"], text="北京")
            await runtime.act(action="click", ref_id=refs["查询"])
            snap2 = await runtime.snapshot(30)
            assert "结果:北京" in snap2["text"]
            extracted = await runtime.extract()
            assert Path(extracted["path"]).is_file()
            assert "北京" in extracted["preview"]
            assert "| 城市 | 金额 |" in extracted["preview"]
            # refs are scoped to the newest snapshot and cannot be guessed
            with pytest.raises(ValueError):
                await runtime.act(action="click", ref_id="e999")
        finally:
            await runtime.close()

    asyncio.run(run())


@requires_chrome
def test_brw04_high_risk_click_requires_approval(local_site: str, tmp_path: Path) -> None:
    async def run() -> None:
        runtime = BrowserRuntime(
            turn_id="brw04",
            profile_dir=tmp_path / "p2",
            media_dir=tmp_path / "m2",
            downloads_dir=tmp_path / "d2",
            allow_private=True,
            headless=True,
        )
        asked: List[Dict[str, Any]] = []

        async def on_approval(tool_name, risk, summary, details, wait_sec):
            asked.append({"tool": tool_name, "summary": summary, "details": details})
            return False, False

        ctx = ToolExecutionContext(
            turn_id="brw04",
            session_id="s",
            config=AppConfig(),
            worker_mandate=WorkerMandate(allowed_packs=["browser_auto"]),
            on_approval=on_approval,
        )
        # Bind this task's runtime registry to the test instance.
        import brain.tools.browser_tools as bt

        original = bt.runtime_for
        original_validate = bt.validate_browser_url
        bt.runtime_for = lambda **kwargs: runtime
        bt.validate_browser_url = lambda *args, **kwargs: str(args[0])
        try:
            await BrowserOpenTool().execute({"url": local_site}, ctx)
            snap = await runtime.snapshot(30)
            refs = {el["name"]: el["ref"] for el in snap["elements"]}
            res = await BrowserActTool().execute(
                {"action": "click", "ref_id": refs["提交订单"]}, ctx
            )
            assert not res.ok
            assert res.status == "blocked"
            assert asked and asked[0]["tool"] == "browser_act"
            assert asked[0]["details"]["preview_image_path"]
            assert Path(asked[0]["details"]["preview_image_path"]).is_file()
        finally:
            bt.runtime_for = original
            bt.validate_browser_url = original_validate
            await runtime.close()

    asyncio.run(run())


@requires_chrome
def test_brw06_live_cdp_binds_existing_tab_and_blocks_cross_domain(
    local_site: str, tmp_path: Path
) -> None:
    import socket
    import subprocess
    import time

    import httpx

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    profile = tmp_path / "live_profile"
    profile.mkdir()
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    proc = subprocess.Popen(
        [
            chrome,
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--headless=new",
            local_site,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        endpoint = f"http://127.0.0.1:{port}"
        ready = False
        for _ in range(40):
            try:
                if httpx.get(f"{endpoint}/json/version", timeout=1).status_code == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.25)
        assert ready, "Chrome CDP endpoint did not start"

        async def run() -> None:
            runtime = BrowserRuntime(
                turn_id="brw06",
                mode="live",
                allowed_domains=["127.0.0.1"],
                profile_dir=profile,
                media_dir=tmp_path / "live_media",
                downloads_dir=tmp_path / "live_downloads",
                cdp_endpoint=endpoint,
                allow_private=True,
            )
            try:
                state = await runtime.open(local_site, tab_hint="测试页")
                assert state["url"].startswith("http://127.0.0.1:")
                snap = await runtime.snapshot(20)
                assert any(el["name"] == "查询" for el in snap["elements"])
                with pytest.raises(Exception):
                    await runtime.page.goto("https://example.com/", timeout=5000)
                assert runtime._blocked_navigation
            finally:
                await runtime.close()

        asyncio.run(run())
        # live cleanup must only drop the CDP session, never close Chrome.
        assert proc.poll() is None
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_browser_settings_api_status_and_clear(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from brain.main import create_app

    cfg = AppConfig()
    cfg.memory.sqlite_path = str(tmp_path / "data" / "mem.db")
    app = create_app(config=cfg)
    with TestClient(app) as client:
        status = client.get("/api/browser/status")
        assert status.status_code == 200
        payload = status.json()
        assert payload["status"] == "ok"
        assert "chrome_installed" in payload
        assert "playwright_installed" in payload
        assert payload["profile_dir"].endswith("data/browser/browser_profile")

        probe = client.post("/api/browser/test_cdp", json={"endpoint": "http://127.0.0.1:1"})
        assert probe.status_code == 200
        assert probe.json()["ok"] is False

        bad = client.post("/api/browser/test_cdp", json={"endpoint": "http://10.0.0.1:9222"})
        assert bad.status_code == 400

        cleared = client.post("/api/browser/clear_profile")
        assert cleared.status_code == 200
        assert cleared.json()["status"] == "ok"


def test_browser_config_roundtrip(tmp_path: Path) -> None:
    import yaml

    from brain.config import load_config, save_settings_to_yaml

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("default_profile: mock_local\n", encoding="utf-8")
    cfg = save_settings_to_yaml(
        {"browser": {"cdp_endpoint": "http://127.0.0.1:9333", "headless": False}},
        config_path=cfg_path,
    )
    assert cfg.browser.cdp_endpoint == "http://127.0.0.1:9333"
    assert cfg.browser.headless is False
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["browser"]["cdp_endpoint"] == "http://127.0.0.1:9333"
    reloaded = load_config(cfg_path)
    assert reloaded.browser.cdp_endpoint == "http://127.0.0.1:9333"


def test_browser_handoff_waits_for_user_and_resumes() -> None:
    from brain.tools.browser_tools import BrowserHandoffTool

    class _FakeRuntime:
        mode = "isolated"
        allowed_domains: List[str] = []

        def __init__(self) -> None:
            self.brought = False

        async def bring_to_front(self) -> None:
            self.brought = True

        async def snapshot(self, max_elements: int = 80) -> Dict[str, Any]:
            return {"title": "已登录", "url": "https://example.com/home", "snapshot_hash": "h"}

    runtime = _FakeRuntime()
    questions: List[str] = []

    async def on_ask_user(question: str, options: List[str], timeout_sec: int):
        questions.append(question)
        return "已完成，继续"

    ctx = ToolExecutionContext(
        turn_id="handoff",
        session_id="s",
        config=AppConfig(),
        worker_mandate=WorkerMandate(allowed_packs=["browser_auto"]),
        on_ask_user=on_ask_user,
    )
    import brain.tools.browser_tools as bt

    original = bt.runtime_for
    bt.runtime_for = lambda **kwargs: runtime
    try:
        result = asyncio.run(
            BrowserHandoffTool().execute(
                {"reason": "需要登录", "instruction": "请登录后台"}, ctx
            )
        )
        assert result.ok
        assert runtime.brought
        assert questions and "登录后台" in questions[0]
        assert "已登录" in result.output
    finally:
        bt.runtime_for = original
