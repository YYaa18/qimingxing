from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from brain.config import AppConfig, MCPServerConfig, ProfileConfig, load_config
from brain.main import create_app
from brain.tools.mcp_client import MCPClientManager
from brain.tools.mcp_oauth import FileTokenStorage, MCPOAuthCoordinator
from brain.tools.registry import ToolRegistry


async def test_file_token_storage_0600_roundtrip(tmp_path: Path) -> None:
    store = FileTokenStorage("t3_code", base_dir=tmp_path)
    assert store.has_tokens_sync() is False
    assert await store.get_tokens() is None
    assert await store.get_client_info() is None

    tok = OAuthToken(access_token="tok_abc_1234", token_type="Bearer", refresh_token="ref_999")
    cinfo = OAuthClientInformationFull(
        client_id="dyn_client_1",
        redirect_uris=[AnyUrl("http://127.0.0.1:18790/api/mcp/oauth/callback")],
        token_endpoint_auth_method="none",
        client_name="Qimingxing Desktop Assistant",
    )
    await store.set_tokens(tok)
    await store.set_client_info(cinfo)

    assert (os.stat(store.path).st_mode & 0o777) == 0o600
    assert store.has_tokens_sync() is True
    loaded_tok = await store.get_tokens()
    loaded_cinfo = await store.get_client_info()
    assert loaded_tok is not None and loaded_tok.access_token == "tok_abc_1234"
    assert loaded_cinfo is not None and loaded_cinfo.client_id == "dyn_client_1"

    store.clear_sync()
    assert store.has_tokens_sync() is False


async def test_oauth_server_without_token_boots_cleanly_as_needs_auth(tmp_path: Path) -> None:
    coord = MCPOAuthCoordinator(storage_dir=tmp_path, open_browser=False)
    cfg = AppConfig(
        mcp_servers=[
            MCPServerConfig(
                name="t3_code",
                enabled=True,
                transport="streamable_http",
                url="https://prod-c162d3c331404d09.t3coderelay.com/mcp",
                auth_mode="oauth",
            )
        ]
    )
    mgr = MCPClientManager(cfg, oauth_coordinator=coord)
    statuses = await mgr.start_all(ToolRegistry())
    assert len(statuses) == 1
    assert statuses[0].name == "t3_code"
    assert statuses[0].status == "needs_auth"
    assert statuses[0].has_oauth_token is False


def test_mcp_crud_api_and_oauth_callback_endpoint(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "default_profile": "mock_local",
                "profiles": {
                    "mock_local": {"label": "Mock", "provider": "mock", "model": "mock"}
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    cfg.memory.sqlite_path = str(tmp_path / "mcp_crud.db")
    cfg.memory.extraction.enabled = False
    cfg.audio.enabled = False

    oauth_dir = tmp_path / "oauth_tokens"
    coord = MCPOAuthCoordinator(storage_dir=oauth_dir, open_browser=False)
    mgr = MCPClientManager(cfg, oauth_coordinator=coord)
    app = create_app(
        config=cfg,
        settings_config_path=cfg_path,
        mcp_manager=mgr,
        oauth_coordinator=coord,
    )

    with TestClient(app) as client:
        # 1. Create remote OAuth MCP via HTTP API
        res_post = client.post(
            "/api/mcp/servers",
            json={
                "name": "t3_code",
                "enabled": True,
                "transport": "streamable_http",
                "auth_mode": "oauth",
                "url": "https://prod-c162d3c331404d09.t3coderelay.com/mcp",
                "headers": {"X-Custom-Key": "secret-hdr-9876"},
            },
        )
        assert res_post.status_code == 200
        servers = res_post.json()["servers"]
        assert len(servers) == 1
        assert servers[0]["name"] == "t3_code"
        assert servers[0]["status"] == "needs_auth"
        assert "secret-hdr-9876" not in res_post.text
        assert servers[0]["headers"]["X-Custom-Key"] == "secr****9876"

        # 2. Updating with masked header preserves original secret in YAML (0600)
        res_update = client.post(
            "/api/mcp/servers",
            json={
                "name": "t3_code",
                "enabled": True,
                "transport": "streamable_http",
                "auth_mode": "oauth",
                "url": "https://prod-c162d3c331404d09.t3coderelay.com/mcp",
                "safe_tools": "read_file, list_dir",
                "headers": {"X-Custom-Key": "secr****9876"},
            },
        )
        assert res_update.status_code == 200
        reloaded = load_config(cfg_path)
        assert reloaded.mcp_servers[0].headers["X-Custom-Key"] == "secret-hdr-9876"
        assert reloaded.mcp_servers[0].safe_tools == ["read_file", "list_dir"]
        assert (os.stat(cfg_path).st_mode & 0o777) == 0o600

        # 3. GET lists merged state cleanly
        res_get = client.get("/api/mcp/servers")
        assert res_get.status_code == 200
        assert res_get.json()["servers"][0]["auth_mode"] == "oauth"

        # 4. OAuth callback validates missing state gracefully (400 HTML)
        res_cb = client.get("/api/mcp/oauth/callback?code=fake&state=unknown_state")
        assert res_cb.status_code == 400
        assert "未找到" in res_cb.text

        # 5. Delete removes from config.yaml and clears state
        res_del = client.delete("/api/mcp/servers/t3_code")
        assert res_del.status_code == 200
        assert res_del.json()["servers"] == []
        assert load_config(cfg_path).mcp_servers == []


async def test_oauth_coordinator_callback_resolution(tmp_path: Path) -> None:
    coord = MCPOAuthCoordinator(storage_dir=tmp_path, open_browser=False)
    provider = coord.build_provider(
        "t3_code",
        "https://prod-c162d3c331404d09.t3coderelay.com/mcp",
        interactive=True,
        auth_timeout_sec=5.0,
    )
    assert provider.context.redirect_handler is not None
    await provider.context.redirect_handler(
        "https://auth.example.com/authorize?state=xyz_state_123&code_challenge=abc"
    )
    assert "t3_code" in coord.pending_auth_url

    wait_task = asyncio.create_task(provider.context.callback_handler())
    await asyncio.sleep(0.05)
    ok, _msg = coord.complete_callback(code="auth_code_777", state="xyz_state_123", iss=None)
    assert ok is True
    result = await wait_task
    assert result.code == "auth_code_777"
    assert result.state == "xyz_state_123"
