from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from brain.config import AppConfig, load_config
from brain.main import create_app


def test_settings_api_masks_keys_saves_0600_and_hot_reloads(tmp_path: Path) -> None:
    cfg_path = tmp_path / "user_config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "default_profile": "deepseek_flash",
                "profiles": {
                    "deepseek_flash": {
                        "label": "DeepSeek Flash",
                        "provider": "openai_compatible",
                        "model": "deepseek-chat",
                        "base_url": "https://api.deepseek.com/v1",
                        "api_key": "sk-initial-secret-1234",
                    },
                    "mock_local": {
                        "label": "Mock",
                        "provider": "mock",
                        "model": "mock-v1",
                    },
                },
                "ocr": {
                    "provider": "deepseek_ocr",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "sk-initial-secret-1234",
                },
                "privacy": {"default_mode": "meta_only"},
                "audio": {
                    "enabled": True,
                    "tts": {"voice": "stella"},
                    "ptt": {"hotkey": "right_option"},
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)
    cfg.memory.sqlite_path = str(tmp_path / "settings_test.db")
    cfg.memory.extraction.enabled = False

    app = create_app(config=cfg, settings_config_path=cfg_path)

    with TestClient(app) as client:
        # 1. GET masks secrets strictly
        r_get = client.get("/api/config/settings")
        assert r_get.status_code == 200
        raw_json = r_get.text
        assert "sk-initial-secret-1234" not in raw_json
        body = r_get.json()
        assert body["shared_deepseek_key"]["has_key"] is True
        assert body["shared_deepseek_key"]["masked"] == "sk-i****1234"
        assert body["profiles"]["deepseek_flash"]["key_masked"] == "sk-i****1234"

        with client.websocket_connect("/ws") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "h1",
                        "ts": 1,
                        "source": "body",
                        "type": "system.hello",
                        "payload": {"client_name": "body_godot", "role": "body"},
                    }
                )
            )
            while json.loads(ws.receive_text())["type"] != "system.privacy_state":
                pass

            # 2. POST updates shared key + switches default profile + privacy + voice/hotkey
            r_post = client.post(
                "/api/config/settings",
                json={
                    "default_profile": "mock_local",
                    "shared_deepseek_api_key": "sk-updated-newkey-9999",
                    "privacy": {"default_mode": "full_on_demand"},
                    "audio": {
                        "enabled": True,
                        "tts": {"voice": "tide", "enabled": False},
                        "ptt": {"hotkey": "option+space"},
                    },
                    "batch_context": {"inbox_dir": "~/Desktop/inbox_custom"},
                    "godot": {"auto_hide_on_capture": False, "earcons_enabled": False},
                },
            )
            assert r_post.status_code == 200
            assert "sk-updated-newkey-9999" not in r_post.text
            res_body = r_post.json()
            assert res_body["status"] == "ok"
            assert res_body["settings"]["default_profile"] == "mock_local"
            assert res_body["settings"]["shared_deepseek_key"]["masked"] == "sk-u****9999"
            assert res_body["settings"]["audio"]["tts"]["voice"] == "tide"
            assert res_body["settings"]["audio"]["tts"]["enabled"] is False
            assert res_body["settings"]["audio"]["ptt"]["hotkey"] == "option+space"
            assert res_body["settings"]["privacy"]["default_mode"] == "full_on_demand"
            assert res_body["settings"]["godot"]["auto_hide_on_capture"] is False

            # Hub broadcast updated welcome & privacy_state live
            seen_types = []
            for _ in range(2):
                msg = json.loads(ws.receive_text())
                seen_types.append(msg["type"])
                if msg["type"] == "system.welcome":
                    assert msg["payload"]["active_profile"] == "mock_local"
                elif msg["type"] == "system.privacy_state":
                    assert msg["payload"]["mode"] == "full_on_demand"
            assert set(seen_types) == {"system.welcome", "system.privacy_state"}

        # 3. Empty key OR masked placeholder preserves existing updated key rather than wiping it
        r_keep = client.post(
            "/api/config/settings",
            json={
                "shared_deepseek_api_key": "sk-u****9999",
                "profiles": {"deepseek_flash": {"model": "deepseek-v4-pro", "api_key": "****"}},
                "audio": {"tts": {"api_key": "sk-****9999"}},
            },
        )
        assert r_keep.status_code == 200
        kept = r_keep.json()["settings"]
        assert kept["profiles"]["deepseek_flash"]["model"] == "deepseek-v4-pro"
        assert kept["profiles"]["deepseek_flash"]["key_masked"] == "sk-u****9999"

    # 4. File permission must be 0o600
    assert (os.stat(cfg_path).st_mode & 0o777) == 0o600
    reloaded = load_config(cfg_path)
    assert reloaded.profiles["deepseek_flash"].api_key == "sk-updated-newkey-9999"
    assert reloaded.ocr.api_key == "sk-updated-newkey-9999"
    assert reloaded.audio.tts.api_key == "sk-updated-newkey-9999"


def test_feedback_endpoint_appends_jsonl(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.memory.sqlite_path = str(tmp_path / "memory.db")
    app = create_app(config=cfg)

    with TestClient(app) as client:
        res = client.post(
            "/api/feedback",
            json={
                "turn_id": "turn_feedback",
                "rating": "up",
                "profile": "mock_local",
                "prompt": "介绍一下你自己",
                "answer": "我是启明星。",
            },
        )
        assert res.status_code == 200
        assert res.json()["status"] == "ok"

        bad = client.post("/api/feedback", json={"turn_id": "t", "rating": "meh"})
        assert bad.status_code == 400

    with sqlite3.connect(tmp_path / "memory.db") as conn:
        row = conn.execute(
            "SELECT turn_id, rating, answer FROM message_feedback"
        ).fetchone()
    assert row == ("turn_feedback", "up", "我是启明星。")
