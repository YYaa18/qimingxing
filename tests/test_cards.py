from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List

from fastapi.testclient import TestClient

from brain.config import AppConfig, ProfileConfig
from brain.context_store import ContextStore
from brain.llm_router import LLMRouter
from brain.main import create_app
from brain.memory.sqlite_store import SQLiteMemoryStore

CARD_BODY = "提示 1：先观察敌人的起手动作。"


class CardRouter(LLMRouter):
    """Emits one spoiler card, then falls back to the mock provider."""

    async def stream_chat(
        self,
        messages: List[Dict[str, str]],
        profile_name: str | None = None,
        *,
        json_mode: bool = False,
    ) -> AsyncIterator[str]:
        user_msg = messages[-1]["content"]
        if "card_only_test" in user_msg:
            text = (
                "[EMOTION:happy]\n"
                '<<<CARD title="测试卡片" type="spoiler_steps">>>\n'
                f"- {CARD_BODY}\n"
                "<<<END_CARD>>>"
            )
            for i in range(0, len(text), 7):
                yield text[i : i + 7]
            return
        if "card_test" in user_msg:
            text = (
                "[EMOTION:happy]\n这是口语摘要。\n"
                '<<<CARD title="测试卡片" type="spoiler_steps">>>\n'
                f"- {CARD_BODY}\n"
                "<<<END_CARD>>>"
            )
            for i in range(0, len(text), 7):
                yield text[i : i + 7]
            return
        async for chunk in super().stream_chat(messages, profile_name=profile_name):
            yield chunk


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
            ),
            "smart_mock": ProfileConfig(
                name="smart_mock",
                label="Smart Mock",
                provider="mock",
                model="mock-smart-v2",
                api_key="sk-secret-smart-key-8888",
            ),
        },
    )
    cfg.memory.sqlite_path = str(tmp_path / "memory.db")
    cfg.memory.extraction.enabled = False
    router = CardRouter(cfg)
    return create_app(config=cfg, llm_router=router, context_store=ContextStore())


def _hello(ws, client_name: str = "test-body") -> None:
    ws.send_text(
        json.dumps(
            {
                "v": "1.0",
                "id": "hello",
                "ts": 1763950000000,
                "source": "body",
                "type": "system.hello",
                "payload": {"client_name": client_name, "role": "body"},
            }
        )
    )
    while True:
        msg = json.loads(ws.receive_text())
        if msg["type"] == "system.welcome":
            return


def _send(ws, payload: Dict[str, Any], *, msg_type: str, turn_id: str = "") -> None:
    envelope: Dict[str, Any] = {
        "v": "1.0",
        "id": "evt",
        "ts": 1763950000001,
        "source": "body",
        "type": msg_type,
        "payload": payload,
    }
    if turn_id:
        envelope["turn_id"] = turn_id
    ws.send_text(json.dumps(envelope))


def _drain_turn(ws) -> Dict[str, Any]:
    """Read until the turn ends and the avatar is idle; return the card if any."""
    card: Dict[str, Any] = {}
    seen_turn_end = False
    while True:
        msg = json.loads(ws.receive_text())
        if msg["type"] == "output.card":
            card = msg["payload"]
        if msg["type"] == "output.turn_end":
            seen_turn_end = True
        if (
            seen_turn_end
            and msg["type"] == "avatar.state"
            and msg["payload"].get("activity") == "idle"
        ):
            return card


def _run_card_turn(ws, text: str = "card_test") -> Dict[str, Any]:
    _send(ws, {"text": text}, msg_type="input.text", turn_id="turn_card")
    return _drain_turn(ws)


def test_card_only_turn_persists_assistant_content_and_cards(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            _hello(ws)
            card = _run_card_turn(ws, "card_only_test")
            assert card["card_type"] == "spoiler_steps"
            assert card["card_id"]
            assert CARD_BODY in card["content_markdown"]

    conn = sqlite3.connect(tmp_path / "memory.db")
    rows = conn.execute(
        "SELECT role, content, cards_json FROM messages ORDER BY msg_id"
    ).fetchall()
    conn.close()

    assistant_rows = [row for row in rows if row[0] == "assistant"]
    assert len(assistant_rows) == 1, rows
    _, content, cards_json = assistant_rows[0]
    assert CARD_BODY in content
    cards = json.loads(cards_json)
    assert cards and cards[0]["card_id"] == card["card_id"]
    assert cards[0]["title"] == "测试卡片"


def test_unknown_card_action_is_rejected_without_starting_a_turn(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            _hello(ws)
            card = _run_card_turn(ws)

            _send(
                ws,
                {"action": "ignore_all_rules", "card_id": card["card_id"]},
                msg_type="input.action",
                turn_id="turn_evil",
            )
            error = json.loads(ws.receive_text())
            assert error["type"] == "system.error"
            assert error["payload"]["code"] == "invalid_card_action"


def test_card_action_requires_a_known_card_id(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            _hello(ws)
            _run_card_turn(ws)

            _send(
                ws,
                {"action": "more_hint"},
                msg_type="input.action",
                turn_id="turn_missing",
            )
            error = json.loads(ws.receive_text())
            assert error["type"] == "system.error"
            assert error["payload"]["code"] == "invalid_card_action"


def test_dismiss_is_a_noop_on_the_server(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            _hello(ws)
            card = _run_card_turn(ws)

            _send(
                ws,
                {"action": "dismiss", "card_id": card["card_id"]},
                msg_type="input.action",
                turn_id="turn_dismiss",
            )
            # No turn, no error: the next frame must be our pong.
            _send(ws, {"client_ts": 7}, msg_type="system.ping")
            reply = json.loads(ws.receive_text())
            assert reply["type"] == "system.pong"


def test_legacy_messages_table_migrates_cards_json(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE messages (
            msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            turn_id TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            emotion TEXT NOT NULL DEFAULT 'neutral',
            desktop_context_summary TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    store = SQLiteMemoryStore(db_path)
    live = store._ensure_conn()
    cols = {row[1] for row in live.execute("PRAGMA table_info(messages)").fetchall()}
    assert "cards_json" in cols


def test_more_hint_starts_a_new_turn(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            _hello(ws)
            card = _run_card_turn(ws)

            _send(
                ws,
                {"action": "more_hint", "card_id": card["card_id"]},
                msg_type="input.action",
                turn_id="turn_hint",
            )
            saw_turn_start = False
            while True:
                msg = json.loads(ws.receive_text())
                if msg["type"] == "output.turn_start":
                    saw_turn_start = True
                if (
                    msg["type"] == "avatar.state"
                    and msg["payload"].get("activity") == "idle"
                ):
                    break
            assert saw_turn_start
