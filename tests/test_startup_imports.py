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
