from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi.testclient import TestClient

from brain.config import (
    AppConfig,
    AudioConfig,
    ProfileConfig,
    get_sanitized_settings,
    load_config,
    save_settings_to_yaml,
)
from brain.main import create_app
from senses.audio.base import _MOD_FLAG_ALT, is_disabled_hotkey
from senses.audio.macos_ptt import MacOSPTTListener
from senses.main import SensesAgent


def _recv_until(ws: Any, expected_type: str) -> Dict[str, Any]:
    while True:
        msg = json.loads(ws.receive_text())
        if msg["type"] == expected_type:
            return msg


def test_shortcuts_config_load_sanitize_and_save_sync(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("default_profile: mock_local\n", encoding="utf-8")

    cfg = load_config(cfg_path)
    assert cfg.shortcuts.ptt_hotkey == "right_option"
    assert cfg.shortcuts.chat_input_hotkey == "option+space"
    assert cfg.shortcuts.cancel_hotkey == "option+esc"
    assert cfg.shortcuts.toggle_dock_hotkey == "option+h"
    assert cfg.shortcuts.settings_hotkey == "option+k"

    sanitized = get_sanitized_settings(cfg)
    assert sanitized["shortcuts"]["chat_input_hotkey"] == "option+space"

    updated = save_settings_to_yaml(
        {
            "shortcuts": {
                "ptt_hotkey": "disabled",
                "chat_input_hotkey": "ctrl+space",
                "cancel_hotkey": "f4",
                "toggle_dock_hotkey": "option+d",
                "settings_hotkey": "option+s",
            }
        },
        config_path=cfg_path,
    )
    assert updated.shortcuts.ptt_hotkey == "disabled"
    assert updated.audio.ptt.hotkey == "disabled"
    assert updated.shortcuts.chat_input_hotkey == "ctrl+space"
    assert updated.shortcuts.cancel_hotkey == "f4"
    assert updated.shortcuts.toggle_dock_hotkey == "option+d"
    assert updated.shortcuts.settings_hotkey == "option+s"


def test_macos_listener_multi_action_and_live_reload() -> None:
    # On macOS the native SwiftUI shell owns every UI hotkey; Senses only
    # keeps PTT, so its action map must be empty.
    assert SensesAgent._platform_action_hotkeys({"chat_input": "option+space"}) == {}
    assert is_disabled_hotkey("disabled") is True
    assert is_disabled_hotkey("option+space") is False

    ptt_events: List[str] = []
    actions_fired: List[Tuple[str, str]] = []
    listener = MacOSPTTListener(
        "disabled",
        action_hotkeys={
            "chat_input": "option+space",
            "cancel": "option+esc",
            "disabled_one": "disabled",
        },
        on_action=lambda act, hk: actions_fired.append((act, hk)),
    )
    listener._on_press = lambda: ptt_events.append("press")
    listener._on_release = lambda: ptt_events.append("release")

    # Right option FlagsChanged should not trigger PTT while disabled
    listener._handle_event(12, 61, _MOD_FLAG_ALT, 0)
    assert ptt_events == []

    # Option+Space KeyDown (49) -> triggers chat_input once; autorepeat ignored until KeyUp
    listener._handle_event(10, 49, _MOD_FLAG_ALT, 0)
    listener._handle_event(10, 49, _MOD_FLAG_ALT, 1)
    assert actions_fired == [("chat_input", "option+space")]
    listener._handle_event(11, 49, _MOD_FLAG_ALT, 0)
    listener._handle_event(10, 49, _MOD_FLAG_ALT, 0)
    assert actions_fired == [
        ("chat_input", "option+space"),
        ("chat_input", "option+space"),
    ]
    listener._handle_event(11, 49, 0, 0)

    # Hot-reload PTT + new chat_input shortcut
    listener.update_hotkeys(
        "right_option",
        {"chat_input": "f2", "toggle_dock": "option+h"},
    )
    listener._handle_event(12, 61, _MOD_FLAG_ALT, 0)
    listener._handle_event(12, 61, 0, 0)
    assert ptt_events == ["press", "release"]

    # F2 (keycode 120) without modifiers
    listener._handle_event(10, 120, 0, 0)
    assert actions_fired[-1] == ("chat_input", "f2")


def test_hub_shortcuts_sync_and_trigger_routing(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("default_profile: mock_local\n", encoding="utf-8")

    cfg = AppConfig(
        default_profile="mock_local",
        profiles={
            "mock_local": ProfileConfig(
                name="mock_local",
                label="Mock",
                provider="mock",
                model="mock",
            )
        },
        audio=AudioConfig(enabled=False),
    )
    cfg.memory.sqlite_path = str(tmp_path / "shortcuts_hub.db")
    cfg.memory.extraction.enabled = False
    cfg.tools.enabled = False

    app = create_app(config=cfg, settings_config_path=cfg_path)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as body_ws, client.websocket_connect("/ws") as senses_ws:
            body_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "hb",
                        "ts": 1,
                        "source": "body",
                        "type": "system.hello",
                        "payload": {"client_name": "body_godot", "role": "body", "client_type": "body_godot"},
                    }
                )
            )
            welcome_b = _recv_until(body_ws, "system.welcome")
            assert welcome_b["payload"]["shortcuts"]["chat_input_hotkey"] == "option+space"
            _recv_until(body_ws, "system.privacy_state")

            senses_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "hs",
                        "ts": 2,
                        "source": "senses",
                        "type": "system.hello",
                        "payload": {"client_name": "senses", "role": "senses"},
                    }
                )
            )
            welcome_s = _recv_until(senses_ws, "system.welcome")
            assert welcome_s["payload"]["shortcuts"]["ptt_hotkey"] == "right_option"
            _recv_until(senses_ws, "system.privacy_state")
            _recv_until(body_ws, "system.privacy_state")

            # POST /api/config/settings updates and broadcasts new shortcuts
            resp = client.post(
                "/api/config/settings",
                json={
                    "shortcuts": {
                        "ptt_hotkey": "disabled",
                        "chat_input_hotkey": "option+t",
                    }
                },
            )
            assert resp.status_code == 200
            welcome_b2 = _recv_until(body_ws, "system.welcome")
            sync_s2 = _recv_until(senses_ws, "system.shortcuts_sync")
            assert welcome_b2["payload"]["shortcuts"]["chat_input_hotkey"] == "option+t"
            assert welcome_b2["payload"]["shortcuts"]["ptt_hotkey"] == "disabled"
            assert sync_s2["payload"]["chat_input_hotkey"] == "option+t"
            assert sync_s2["payload"]["ptt_hotkey"] == "disabled"

            # Global shortcut event from Senses reaches Godot Body
            senses_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "hk1",
                        "ts": 3,
                        "source": "senses",
                        "type": "system.hotkey_triggered",
                        "payload": {"action": "chat_input", "hotkey": "option+t"},
                    }
                )
            )
            trig_msg = _recv_until(body_ws, "system.hotkey_triggered")
            assert trig_msg["payload"]["action"] == "chat_input"
            assert trig_msg["payload"]["hotkey"] == "option+t"
