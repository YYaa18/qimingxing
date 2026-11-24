from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from brain.artifact_store import ArtifactStore
from brain.config import AppConfig, ProfileConfig
from brain.main import create_app
from senses.privacy_guard import MASKED_PLACEHOLDER, PrivacyGuard
from senses.window_providers import (
    ActiveWindowInfo,
    MacOSWindowProvider,
    WindowsWindowProvider,
    create_window_provider,
)


def test_privacy_guard_three_modes_and_blacklist() -> None:
    guard = PrivacyGuard(mode="off")
    assert guard.allows_window_reporting() is False
    allowed, reason = guard.can_capture(ActiveWindowInfo(app_name="VS Code", window_title="main.py"))
    assert not allowed
    assert reason == "permission_denied"

    guard.set_mode("meta_only")
    assert guard.allows_window_reporting() is True
    allowed, reason = guard.can_capture(ActiveWindowInfo(app_name="VS Code", window_title="main.py"))
    assert not allowed
    assert reason == "permission_denied"

    # Blacklist masking in meta_only
    masked_payload = guard.sanitize_window(
        ActiveWindowInfo(app_name="1Password", window_title="My Vault - Passwords")
    )
    assert masked_payload.privacy_masked is True
    assert masked_payload.app_name == MASKED_PLACEHOLDER
    assert masked_payload.window_title == MASKED_PLACEHOLDER
    assert "1Password" not in masked_payload.app_name
    assert "My Vault" not in masked_payload.window_title

    # Title keyword blacklist (e.g. Incognito / 网银)
    masked_incognito = guard.sanitize_window(
        ActiveWindowInfo(app_name="Google Chrome", window_title="Search - Incognito")
    )
    assert masked_incognito.privacy_masked is True

    # full_on_demand allows normal windows, blocks blacklisted windows
    guard.set_mode("full_on_demand")
    ok_win = ActiveWindowInfo(app_name="VS Code", window_title="hub.py")
    assert guard.can_capture(ok_win) == (True, "")

    blocked_win = ActiveWindowInfo(app_name="Safari", window_title="招商银行个人网银")
    assert guard.can_capture(blocked_win) == (False, "privacy_blocked")


def test_window_providers_macos_windows_and_factory() -> None:
    mac_provider = MacOSWindowProvider(
        app_fetcher=lambda: ("Code", 1234, "com.microsoft.VSCode"),
        title_fetcher=lambda pid: "启明星 · Qimingxing" if pid == 1234 else "",
        screen_perm_checker=lambda: False,
    )
    assert mac_provider.platform_name == "macos"
    win = mac_provider.get_active_window()
    assert win is not None
    assert win.app_name == "Code"
    assert win.is_self_window is True
    perm = mac_provider.check_permission()
    assert perm.window_meta is True
    assert perm.screen_capture is False
    assert "屏幕录制" in perm.permission_hint

    win_provider = WindowsWindowProvider(
        foreground_fetcher=lambda: ("Code", "Code.exe", "stream_parser.py - qimingxing")
    )
    assert win_provider.platform_name == "windows"
    w_info = win_provider.get_active_window()
    assert w_info is not None
    assert w_info.app_name == "Code"
    assert w_info.process_name == "Code.exe"
    assert w_info.is_self_window is True
    assert win_provider.check_permission().screen_capture is True

    assert isinstance(create_window_provider("darwin"), MacOSWindowProvider)
    assert isinstance(create_window_provider("win32"), WindowsWindowProvider)


def test_artifact_store_save_burn_ttl_and_http_upload(tmp_path: Path) -> None:
    store = ArtifactStore(base_dir=tmp_path, max_bytes=1024, ttl_seconds=10)
    rec = store.save(b"\xff\xd8\xff\xe0jpegbytes", content_type="image/jpeg")
    assert rec.path.exists()
    assert store.read_bytes(rec.artifact_id) == (b"\xff\xd8\xff\xe0jpegbytes", "image/jpeg")

    # Physical deletion
    assert store.delete(rec.artifact_id) is True
    assert not rec.path.exists()
    assert store.read_bytes(rec.artifact_id) is None

    # TTL cleanup fallback
    rec2 = store.save(b"abc", content_type="image/jpeg")
    assert rec2.path.exists()
    assert store.cleanup_expired(now=time.time() + 20) == 1
    assert not rec2.path.exists()

    # Over-limit HTTP 413 and valid upload via FastAPI
    cfg = AppConfig(
        default_profile="mock_local",
        profiles={
            "mock_local": ProfileConfig(
                name="mock_local", label="Mock", provider="mock", model="mock-v1"
            )
        },
    )
    cfg.memory.sqlite_path = str(tmp_path / "test_artifacts.db")
    app = create_app(config=cfg, artifact_store=ArtifactStore(base_dir=tmp_path, max_bytes=100))
    with TestClient(app) as client:
        r_ok = client.post(
            "/api/artifacts",
            files={"file": ("shot.jpg", b"1234567890", "image/jpeg")},
        )
        assert r_ok.status_code == 200
        aid = r_ok.json()["artifact_id"]
        assert app.state.artifact_store.get_path(aid).exists()
        app.state.artifact_store.delete(aid)

        r_big = client.post(
            "/api/artifacts",
            files={"file": ("big.jpg", b"x" * 200, "image/jpeg")},
        )
        assert r_big.status_code == 413

        # Verify static hosting of 3-file body_web
        assert client.get("/").status_code == 200
        assert client.get("/app.js").status_code == 200
        assert client.get("/styles.css").status_code == 200
