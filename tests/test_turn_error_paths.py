"""Turn failure paths must always terminate the turn (no stuck 'generating' UI)."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from brain.config import AppConfig
from brain.main import create_app


def _hello(ws) -> None:
    ws.send_text(
        json.dumps(
            {
                "v": "1.0",
                "id": "hello",
                "ts": 1,
                "source": "body",
                "type": "system.hello",
                "payload": {"client_name": "test", "role": "body"},
            }
        )
    )
    while True:
        message = json.loads(ws.receive_text())
        if message["type"] == "system.welcome":
            return


def test_invalid_profile_still_sends_error_and_turn_end(tmp_path) -> None:
    cfg = AppConfig()
    cfg.audio.enabled = False
    cfg.memory.sqlite_path = str(tmp_path / "memory.db")
    cfg.profiles = {}
    cfg.default_profile = "missing"

    app = create_app(config=cfg)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            _hello(ws)
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "turn",
                        "ts": 2,
                        "source": "body",
                        "type": "input.text",
                        "turn_id": "turn_bad_profile",
                        "payload": {"text": "你好"},
                    }
                )
            )

            saw_error = False
            saw_turn_end = False
            for _ in range(12):
                message = json.loads(ws.receive_text())
                if message["type"] == "system.error":
                    saw_error = True
                if (
                    message["type"] == "output.turn_end"
                    and message.get("turn_id") == "turn_bad_profile"
                ):
                    saw_turn_end = True
                    break

            assert saw_error, "missing system.error for invalid profile"
            assert saw_turn_end, "missing output.turn_end; shell would stay in generating state"
