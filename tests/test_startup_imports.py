"""Startup regression guards: importing brain.main must stay cheap and side-effect free."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_import_probe(expression: str) -> dict:
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    env.pop("QIMINGXING_CONFIG", None)
    script = (
        "import json, sys\n"
        f"import brain.main\n"
        f"print(json.dumps({expression}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_import_brain_main_does_not_construct_app() -> None:
    result = _run_import_probe(
        "{"
        "'app_built': brain.main._app is not None, "
        "'app_attr_materialized': 'app' in vars(brain.main)"
        "}"
    )
    assert result == {"app_built": False, "app_attr_materialized": False}


def test_optional_heavy_dependencies_stay_lazy_at_import() -> None:
    result = _run_import_probe(
        "{name: name in sys.modules for name in ("
        "'mcp', 'mcp.client.auth', 'playwright.async_api', 'pypdf'"
        ")}"
    )
    assert result == {
        "mcp": False,
        "mcp.client.auth": False,
        "playwright.async_api": False,
        "pypdf": False,
    }


def test_slow_mcp_server_does_not_block_health(tmp_path) -> None:
    """A hanging optional MCP server must not delay readiness in production mode."""
    import time

    from fastapi.testclient import TestClient

    from brain.config import AppConfig, MCPServerConfig
    from brain.main import create_app

    cfg = AppConfig()
    cfg.audio.enabled = False
    cfg.memory.sqlite_path = str(tmp_path / "memory.db")
    cfg.mcp_servers = [
        MCPServerConfig(
            name="slow",
            enabled=True,
            transport="stdio",
            command="sleep",
            args=["5"],
            startup_timeout_sec=15,
        )
    ]
    # config=... disables the model store (test harness path) but leaves the
    # MCP manager un-injected, which is exactly the production startup path.
    app = create_app(config=cfg)
    started = time.perf_counter()
    with TestClient(app) as client:
        elapsed = time.perf_counter() - started
        health = client.get("/health")
        assert health.status_code == 200
        assert "mcp_starting" in health.json()
    assert elapsed < 3.0, f"lifespan blocked on MCP for {elapsed:.2f}s"
