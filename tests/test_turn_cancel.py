from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List

from fastapi.testclient import TestClient

from brain.config import AppConfig, ProfileConfig
from brain.context_store import ContextStore
from brain.llm_router import LLMError, LLMRouter
from brain.main import create_app


class ControllableRouter(LLMRouter):
    """Test router that tracks upstream cancellation and supports error/switch tests."""

    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self.cancelled_event = asyncio.Event()
        self.stream_started_event = asyncio.Event()
        self.force_error = False

    async def stream_chat(
        self,
        messages: List[Dict[str, str]],
        profile_name: str | None = None,
    ) -> AsyncIterator[str]:
        profile = self.get_profile(profile_name)
        if self.force_error:
            safe = self._config.redact_secrets(
                f"Upstream auth failed using {profile.api_key}"
            )
            raise LLMError("UPSTREAM_HTTP_401", safe)

        user_msg = messages[-1]["content"]
        if "slow_cancel_test" in user_msg:
            self.stream_started_event.set()
            try:
                for idx in range(100):
                    await asyncio.sleep(0.02)
                    yield f"片段{idx} "
            except asyncio.CancelledError:
                self.cancelled_event.set()
                raise

        if "card_test" in user_msg:
            text = (
                "[EMOTION:happy]\n这是口语摘要。\n"
                '<<<CARD title="测试卡片" type="spoiler_steps">>>\n- 提示 1\n<<<END_CARD>>>'
            )
            for i in range(0, len(text), 7):
                yield text[i : i + 7]
            return

        async for chunk in super().stream_chat(messages, profile_name=profile_name):
            yield chunk


def _make_test_app():
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
    router = ControllableRouter(cfg)
    ctx = ContextStore()
    app = create_app(config=cfg, llm_router=router, context_store=ctx)
    return app, router


def test_handshake_no_key_leak_and_unknown_event_ignored() -> None:
    app, _ = _make_test_app()
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "evt_1",
                        "ts": 1763950000100,
                        "source": "body",
                        "type": "system.hello",
                        "payload": {"client_name": "test-body", "role": "body"},
                    }
                )
            )
            welcome = json.loads(ws.receive_text())
            assert welcome["type"] == "system.welcome"
            assert welcome["payload"]["active_profile"] == "fast_mock"
            dumped = json.dumps(welcome)
            assert "sk-secret" not in dumped
            assert "api_key" not in dumped

            # Send unimplemented event & extra payload field — must not disconnect
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "evt_2",
                        "ts": 1763950000200,
                        "source": "body",
                        "type": "audio.stream_start",
                        "payload": {"codec": "opus", "extra": 123},
                    }
                )
            )
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "evt_3",
                        "ts": 1763950000300,
                        "source": "body",
                        "type": "system.ping",
                        "payload": {"client_ts": 42, "unknown_field": "ok"},
                    }
                )
            )
            pong = json.loads(ws.receive_text())
            assert pong["type"] == "system.pong"
            assert pong["payload"]["client_ts"] == 42


def test_senses_context_injection_profile_hotswitch_and_cards() -> None:
    app, _ = _make_test_app()
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as senses_ws, client.websocket_connect("/ws") as body_ws:
            # Senses handshake + game_state injection
            senses_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "s_1",
                        "ts": 1763950000100,
                        "source": "senses",
                        "type": "system.hello",
                        "payload": {"client_name": "mock-senses", "role": "senses"},
                    }
                )
            )
            assert json.loads(senses_ws.receive_text())["type"] == "system.welcome"

            senses_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "s_2",
                        "ts": 1763950000200,
                        "source": "senses",
                        "type": "context.game_state",
                        "payload": {
                            "game_id": "elden_ring",
                            "provider": "mock_save_parser",
                            "summary_version": 5,
                            "data": {"location": "Redmane Castle", "level": 68},
                        },
                    }
                )
            )

            # Body handshake
            body_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "b_1",
                        "ts": 1763950000300,
                        "source": "body",
                        "type": "system.hello",
                        "payload": {"client_name": "test-body", "role": "body"},
                    }
                )
            )
            assert json.loads(body_ws.receive_text())["type"] == "system.welcome"

            # Ask about current game state without restarting Brain
            body_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "b_2",
                        "turn_id": "turn_ctx",
                        "ts": 1763950000400,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "我当前角色状态是什么？"},
                    }
                )
            )
            collected_text = []
            while True:
                msg = json.loads(body_ws.receive_text())
                if msg["type"] == "output.text_delta":
                    collected_text.append(msg["payload"]["delta"])
                if msg["type"] == "output.turn_end":
                    assert msg["payload"]["finish_reason"] == "completed"
                    break
            full_reply = "".join(collected_text)
            assert "Redmane Castle" in full_reply
            assert "68" in full_reply

            # Consume final idle avatar.state
            idle_msg = json.loads(body_ws.receive_text())
            assert idle_msg["type"] == "avatar.state"
            assert idle_msg["payload"]["activity"] == "idle"

            # Hot-switch profile to smart_mock
            body_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "b_3",
                        "ts": 1763950000500,
                        "source": "body",
                        "type": "system.config_set",
                        "payload": {"profile": "smart_mock"},
                    }
                )
            )
            updated_welcome = json.loads(body_ws.receive_text())
            assert updated_welcome["type"] == "system.welcome"
            assert updated_welcome["payload"]["active_profile"] == "smart_mock"

            # Trigger card_test and verify output.card separation
            body_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "b_4",
                        "turn_id": "turn_card",
                        "ts": 1763950000600,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "card_test"},
                    }
                )
            )
            card_events = []
            spoken_events = []
            turn_start_model = ""
            while True:
                msg = json.loads(body_ws.receive_text())
                if msg["type"] == "output.turn_start":
                    turn_start_model = msg["payload"]["model"]
                elif msg["type"] == "output.text_delta":
                    spoken_events.append(msg["payload"]["delta"])
                elif msg["type"] == "output.card":
                    card_events.append(msg["payload"])
                elif msg["type"] == "output.turn_end":
                    break
            assert turn_start_model == "mock-smart-v2"
            assert "".join(spoken_events).strip() == "这是口语摘要。"
            assert len(card_events) == 1
            assert card_events[0]["title"] == "测试卡片"


def test_turn_id_cancel_stops_upstream_task() -> None:
    app, router = _make_test_app()
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "evt_h",
                        "ts": 1763950000100,
                        "source": "body",
                        "type": "system.hello",
                        "payload": {"client_name": "test-body", "role": "body"},
                    }
                )
            )
            assert json.loads(ws.receive_text())["type"] == "system.welcome"

            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "evt_q",
                        "turn_id": "turn_long_1",
                        "ts": 1763950000200,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "slow_cancel_test"},
                    }
                )
            )

            # Wait until at least one delta arrives, confirming stream is actively running
            while True:
                msg = json.loads(ws.receive_text())
                if msg["type"] == "output.text_delta":
                    assert msg["turn_id"] == "turn_long_1"
                    break

            # Cancel turn_long_1
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "evt_c",
                        "turn_id": "turn_long_1",
                        "ts": 1763950000300,
                        "source": "body",
                        "type": "input.cancel",
                        "payload": {"reason": "user_stopped"},
                    }
                )
            )

            saw_cancelled_end = False
            for _ in range(20):
                msg = json.loads(ws.receive_text())
                if msg["type"] == "output.turn_end" and msg["payload"]["finish_reason"] == "cancelled":
                    assert msg["turn_id"] == "turn_long_1"
                    saw_cancelled_end = True
                    break

            assert saw_cancelled_end
            assert router.cancelled_event.is_set()


def test_upstream_error_redacts_secret_and_recovers() -> None:
    app, router = _make_test_app()
    router.force_error = True
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "evt_h",
                        "ts": 1763950000100,
                        "source": "body",
                        "type": "system.hello",
                        "payload": {"client_name": "test-body", "role": "body"},
                    }
                )
            )
            assert json.loads(ws.receive_text())["type"] == "system.welcome"

            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "evt_err",
                        "turn_id": "turn_err_1",
                        "ts": 1763950000200,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "hello"},
                    }
                )
            )

            events: List[Dict[str, Any]] = []
            while True:
                msg = json.loads(ws.receive_text())
                events.append(msg)
                if msg["type"] == "avatar.state" and msg["payload"]["activity"] == "idle":
                    break

            err_msgs = [e for e in events if e["type"] == "system.error"]
            assert len(err_msgs) == 1
            assert "sk-secret" not in err_msgs[0]["payload"]["message"]
            assert "***REDACTED***" in err_msgs[0]["payload"]["message"]
            assert any(
                e["type"] == "output.turn_end" and e["payload"]["finish_reason"] == "error"
                for e in events
            )


async def test_openai_compatible_ignores_reasoning_content(monkeypatch) -> None:
    cfg = AppConfig(
        default_profile="reasoner_test",
        profiles={
            "reasoner_test": ProfileConfig(
                name="reasoner_test",
                label="Reasoner Test",
                provider="openai_compatible",
                model="deepseek-reasoner",
                base_url="https://api.deepseek.com/v1",
                api_key="sk-test-dummy-key-1234",
            )
        },
    )
    router = LLMRouter(cfg)

    sse_lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"内部思考过程不应该输出"}}]}',
        'data: {"choices":[{"delta":{"reasoning_content":"继续思考"}}]}',
        'data: {"choices":[{"delta":{"content":"[EMOTION:happy]\\n"}}]}',
        'data: {"choices":[{"delta":{"content":"最终回答在这里。"}}]}',
        "data: [DONE]",
    ]

    class FakeResp:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_lines(self):
            for ln in sse_lines:
                yield ln

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            return FakeResp()

    import brain.llm_router as llm_router_mod

    monkeypatch.setattr(llm_router_mod.httpx, "AsyncClient", FakeClient)

    tokens: List[str] = []
    async for tok in router.stream_chat([{"role": "user", "content": "hi"}]):
        tokens.append(tok)

    joined = "".join(tokens)
    assert "内部思考过程" not in joined
    assert "继续思考" not in joined
    assert "最终回答在这里。" in joined

