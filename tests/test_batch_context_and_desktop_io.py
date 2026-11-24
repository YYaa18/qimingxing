from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from fastapi.testclient import TestClient

from brain.config import AppConfig, BatchContextConfig, ProfileConfig
from brain.hub import Hub
from brain.main import create_app
from brain.tools.base import ToolExecutionContext
from brain.tools.desktop_io import (
    BatchPathContextStore,
    FinderGetSelectionTool,
    InboxScanTool,
)


def test_attach_paths_builds_controlled_tree_filters_secrets_and_preserves_prefix_cache(
    tmp_path: Path,
) -> None:
    proj = tmp_path / "workspace"
    (proj / "a" / "b" / "c" / "d").mkdir(parents=True)
    # 25 regular files + binary/secret-body file that must NEVER be read
    for i in range(24):
        (proj / "a" / f"doc_{i}.txt").write_text(f"body_{i}_DO_NOT_INJECT", encoding="utf-8")
    (proj / "a" / "b" / "c" / "d" / "too_deep.txt").write_text("deep", encoding="utf-8")
    (proj / "note.md").write_bytes(b"SECRET_FILE_CONTENT_SHOULD_NOT_BE_READ\xff\xfe")

    # Sensitive files that must be filtered out strictly
    env_file = proj / ".env"
    env_file.write_text("API_KEY=secret123", encoding="utf-8")
    ssh_dir = proj / ".ssh"
    ssh_dir.mkdir()
    ssh_key = ssh_dir / "id_rsa"
    ssh_key.write_text("PRIVATE KEY", encoding="utf-8")
    pem_file = proj / "prod.pem"
    pem_file.write_text("CERT", encoding="utf-8")

    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="m")},
        batch_context=BatchContextConfig(max_paths=200, max_tree_depth=3),
    )
    cfg.memory.sqlite_path = str(tmp_path / "attach.db")
    cfg.memory.extraction.enabled = False
    app = create_app(config=cfg)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "h",
                        "ts": 1,
                        "source": "body",
                        "type": "system.hello",
                        "payload": {"client_name": "web", "role": "body"},
                    }
                )
            )
            while json.loads(ws.receive_text())["type"] != "system.privacy_state":
                pass

            # Turn 1: baseline turn without attached paths -> record static_prefix_hash
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "t1",
                        "turn_id": "t1",
                        "ts": 2,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "你好"},
                    }
                )
            )
            while json.loads(ws.receive_text())["type"] != "output.turn_end":
                pass
            hash_before = client.get("/api/memory/debug").json()["last_context_stats"][
                "static_prefix_hash"
            ]

            # Attach workspace + sensitive paths via loopback POST /api/context/attach_paths
            resp = client.post(
                "/api/context/attach_paths",
                json={
                    "paths": [str(proj), str(env_file), str(ssh_key), str(pem_file)],
                    "source": "godot_drop",
                    "append": True,
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["accepted"] >= 1
            assert data["filtered"] >= 3
            assert "doc_0.txt" in data["manifest_preview"]
            assert "too_deep.txt" not in data["manifest_preview"]
            assert "SECRET_FILE_CONTENT_SHOULD_NOT_BE_READ" not in data["manifest_preview"]
            assert "DO_NOT_INJECT" not in data["manifest_preview"]
            assert ".env" not in data["manifest_preview"]
            assert "id_rsa" not in data["manifest_preview"]

            # Turn 2: consumes L3 manifest while L0/L1 static prefix stays byte-identical
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "t2",
                        "turn_id": "t2",
                        "ts": 3,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "帮我看看刚挂载的目录"},
                    }
                )
            )
            while json.loads(ws.receive_text())["type"] != "output.turn_end":
                pass
            hash_after = client.get("/api/memory/debug").json()["last_context_stats"][
                "static_prefix_hash"
            ]
            assert hash_after == hash_before


async def test_finder_get_selection_and_inbox_scan_tools(tmp_path: Path) -> None:
    file_ok = tmp_path / "report.md"
    file_ok.write_text("# Report", encoding="utf-8")
    secret_env = tmp_path / ".env"
    secret_env.write_text("SECRET=1", encoding="utf-8")

    def fake_runner(argv: List[str], *, background: bool = False):
        assert argv[0] == "osascript"
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=f"{file_ok}\n{secret_env}\n",
            stderr="",
        )

    cfg = AppConfig(batch_context=BatchContextConfig(inbox_dir=str(tmp_path / "给启明星")))
    ctx = ToolExecutionContext(turn_id="t1", session_id="s1", config=cfg)

    finder = FinderGetSelectionTool(runner=fake_runner)
    res_finder = await finder.execute({}, ctx)
    assert res_finder.ok is True
    assert "report.md" in res_finder.output
    assert ".env" not in res_finder.output

    inbox_dir = tmp_path / "给启明星"
    inbox_dir.mkdir()
    (inbox_dir / "draft.txt").write_text("hello", encoding="utf-8")
    inbox_tool = InboxScanTool(cfg)
    res_inbox = await inbox_tool.execute({}, ctx)
    assert res_inbox.ok is True
    assert "draft.txt" in res_inbox.output

    # Verify desktop_io module contains zero background polling loops
    src = Path(__file__).resolve().parent.parent / "brain" / "tools" / "desktop_io.py"
    text = src.read_text(encoding="utf-8")
    assert "while True" not in text
    assert "asyncio.sleep" not in text


def test_desktop_open_action_endpoints_and_safety(tmp_path: Path) -> None:
    target = tmp_path / 'sample "note".md'
    target.write_text("# Quick Look", encoding="utf-8")
    secret = tmp_path / "credentials.json"
    secret.write_text("{}", encoding="utf-8")

    recorded_cmds: List[tuple[List[str], bool]] = []

    def mock_runner(argv: List[str], *, background: bool = False):
        recorded_cmds.append((list(argv), background))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="m")},
    )
    cfg.memory.sqlite_path = str(tmp_path / "desktop_io.db")
    cfg.memory.extraction.enabled = False
    app = create_app(config=cfg, desktop_command_runner=mock_runner)

    with TestClient(app) as client:
        r1 = client.post("/api/desktop/open_action", json={"action": "quick_look", "path": str(target)})
        assert r1.status_code == 200
        assert recorded_cmds[-1] == (["qlmanage", "-p", str(target)], True)

        r2 = client.post("/api/desktop/open_action", json={"action": "copy_file", "path": str(target)})
        assert r2.status_code == 200
        assert recorded_cmds[-1][0][0] == "osascript"
        assert '\\"' in recorded_cmds[-1][0][2]

        r3 = client.post("/api/desktop/open_action", json={"action": "reveal", "path": str(target)})
        assert r3.status_code == 200
        assert recorded_cmds[-1] == (["open", "-R", str(target)], False)

        r4 = client.post("/api/desktop/open_action", json={"action": "open_external", "path": str(target)})
        assert r4.status_code == 200
        assert recorded_cmds[-1] == (["open", str(target)], True)

        # Sensitive path rejected with 403; missing path 404; bad action 400
        assert client.post("/api/desktop/open_action", json={"action": "quick_look", "path": str(secret)}).status_code == 403
        assert client.post("/api/desktop/open_action", json={"action": "reveal", "path": str(tmp_path / "missing.txt")}).status_code == 404
        assert client.post("/api/desktop/open_action", json={"action": "rm_rf", "path": str(target)}).status_code == 400
