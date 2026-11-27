from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from brain.artifact_store import ArtifactStore
from brain.config import AppConfig, ProfileConfig, ShellToolConfig
from brain.context_store import ContextStore
from brain.llm_router import LLMRouter
from brain.main import create_app
from brain.tools.shell_tool import ShellTool


def _make_app(tmp_path: Path):
    cfg = AppConfig(
        default_profile="fast_mock",
        profiles={
            "fast_mock": ProfileConfig(
                name="fast_mock",
                label="Fast Mock",
                provider="mock",
                model="mock-fast-v1",
                api_key="sk-secret-fast-key-9999",
            )
        },
    )
    cfg.memory.sqlite_path = str(tmp_path / "memory.db")
    cfg.memory.extraction.enabled = False
    return create_app(
        config=cfg,
        llm_router=LLMRouter(cfg),
        context_store=ContextStore(),
    )


def test_non_loopback_host_header_is_rejected(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/health", headers={"host": "evil.com"}).status_code == 403
        assert client.get("/health", headers={"host": "127.0.0.1:18790"}).status_code == 200
        assert client.get("/health", headers={"host": "localhost:18790"}).status_code == 200


def test_cross_site_origin_is_rejected(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        evil = client.post(
            "/api/config/settings",
            json={},
            headers={"origin": "http://evil.com"},
        )
        assert evil.status_code == 403
        null_origin = client.post(
            "/api/config/settings",
            json={},
            headers={"origin": "null"},
        )
        assert null_origin.status_code == 403


def test_native_client_without_origin_is_allowed(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        # No Origin header + JSON body = native client.
        response = client.post("/api/config/settings", json={})
        assert response.status_code not in (403, 415)


def test_plain_text_write_is_rejected(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/mcp/reload",
            content='{"ignored": true}',
            headers={"content-type": "text/plain"},
        )
        assert response.status_code == 415


def test_multipart_upload_still_allowed_for_artifacts(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/artifacts",
            files={"file": ("shot.jpg", b"jpeg-payload", "image/jpeg")},
        )
        assert response.status_code == 200
        assert response.json()["artifact_id"]


def test_oauth_callback_top_level_navigation_is_allowed(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        # Browsers send no Origin on a top-level GET navigation.
        response = client.get("/api/mcp/oauth/callback")
        assert response.status_code != 403


def test_artifact_delete_rejects_path_traversal() -> None:
    root = Path(tempfile.mkdtemp())
    base = root / "artifacts"
    base.mkdir()
    victim = root / "victim.txt"
    victim.write_text("secret", encoding="utf-8")

    store = ArtifactStore(base_dir=base)
    assert store.delete("../victim") is False
    assert victim.exists()


def test_artifact_delete_still_removes_valid_ids() -> None:
    base = Path(tempfile.mkdtemp())
    store = ArtifactStore(base_dir=base)
    record = store.save(b"payload", content_type="text/plain", filename="a.txt")
    assert store.delete(record.artifact_id) is True
    assert not record.path.exists()


def test_redaction_covers_common_credential_formats() -> None:
    cfg = AppConfig(default_profile="p", profiles={})
    redacted = cfg.redact_secrets(
        "AKIAABCDEFGHIJKLMNOP ghp_abcdefghijklmnopqrstuvwxyz0123456789 "
        "github_pat_11ABCDEFG0123456789_abcdefghijklmnop "
        "xoxb-1234567890-abcdefghij " + ("AIza" + "A" * 35)
    )
    for token in ("AKIA", "ghp_", "github_pat_", "xoxb-", "AIza"):
        assert token not in redacted
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"
    assert "BEGIN RSA PRIVATE KEY" not in cfg.redact_secrets(pem)


def test_mcp_stdio_env_filters_parent_secrets(monkeypatch) -> None:
    from brain.tools.mcp_client import _build_stdio_env

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parent-secret")
    env = _build_stdio_env({"MCP_TOKEN": "server-token"})
    assert env["PATH"] == "/usr/bin:/bin"
    assert "OPENAI_API_KEY" not in env
    assert env["MCP_TOKEN"] == "server-token"


def test_mcp_oauth_dir_lives_outside_repo() -> None:
    from brain.tools import mcp_oauth

    repo = Path(__file__).resolve().parent.parent
    resolved = Path(mcp_oauth._DEFAULT_OAUTH_DIR).resolve()
    assert repo not in resolved.parents


def test_legacy_oauth_tokens_are_migrated(tmp_path: Path, monkeypatch) -> None:
    from brain.tools import mcp_oauth

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "github.json").write_text(
        '{"tokens": {"access_token": "t"}}', encoding="utf-8"
    )
    target = tmp_path / "target"
    monkeypatch.setattr(mcp_oauth, "_legacy_oauth_dir", lambda: legacy)

    mcp_oauth._migrate_legacy_tokens(target)

    migrated = target / "github.json"
    assert migrated.exists()
    assert (migrated.stat().st_mode & 0o777) == 0o600


def test_shell_credential_paths_are_hard_blocked() -> None:
    tool = ShellTool(ShellToolConfig(default_cwd=str(Path.cwd())))
    assert tool.classify_risk({"command": "cat ~/.aws/credentials"}) == "blocked"
    assert tool.classify_risk({"command": "cat ~/.kube/config"}) == "blocked"
    assert tool.classify_risk({"command": "cat /etc/passwd"}) == "confirm"


def test_shell_safe_prefix_only_reads_inside_workspace() -> None:
    tool = ShellTool(ShellToolConfig(default_cwd=str(Path.cwd())))
    assert tool.classify_risk({"command": "ls -la"}) == "safe"
    assert tool.classify_risk({"command": "cat README.md"}) == "safe"
    # Same command, but a cwd outside the workspace root.
    assert tool.classify_risk({"command": "ls", "cwd": "/tmp"}) == "confirm"
    # Same command, but an absolute path outside the workspace root.
    assert tool.classify_risk({"command": "cat /etc/hosts"}) == "confirm"
