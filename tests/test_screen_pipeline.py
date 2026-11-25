from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List

from fastapi.testclient import TestClient

from brain.artifact_store import ArtifactStore
from brain.config import AppConfig, OCRConfig, PrivacyConfig, ProfileConfig
from brain.intent_detector import ScreenIntentDetector
from brain.llm_router import LLMRouter
from brain.main import create_app
from brain.screen_router import DeepSeekOCRProvider, OCRProvider, ScreenUnderstandingRouter
from protocol.models import ContextScreenRefPayload


def test_intent_detector_accuracy_and_zero_false_positives() -> None:
    detector = ScreenIntentDetector()
    # General chat must NOT trigger screen capture
    assert detector.detect("今天新加坡天气怎么样") is False
    assert detector.detect("解释一下 Python 装饰器的原理") is False
    assert detector.detect("你好，启明星") is False
    assert detector.detect("我刚才这十几分钟主要在忙什么？") is False

    # Visual references MUST trigger
    assert detector.detect("帮我看看屏幕上这个报错怎么修？") is True
    assert detector.detect("解释一下我现在正在看的这段内容") is True
    assert detector.detect("What is on my screen right now?") is True

    # Explicit user button bypasses NLP check
    assert detector.detect("你好", explicit_action=True) is True


async def test_screen_router_dual_path_vision_and_deepseek_ocr(monkeypatch) -> None:
    app_cfg = AppConfig(
        ocr=OCRConfig(
            provider="deepseek_ocr",
            base_url="https://api.deepseek.com/v1",
            api_key="sk-ocr-dummy-key-1234",
        )
    )

    class FakeOCRResp:
        status_code = 200

        def json(self) -> dict:
            return {"pages": [{"index": 0, "text": "ZeroDivisionError: division by zero"}]}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            assert url == "https://api.deepseek.com/v1/tools/ocr"
            assert kwargs["data"]["purpose"] == "text_recognition"
            return FakeOCRResp()

    import brain.screen_router as sr_mod

    monkeypatch.setattr(sr_mod.httpx, "AsyncClient", FakeAsyncClient)

    ocr = DeepSeekOCRProvider(app_cfg.ocr, app_cfg)
    router = ScreenUnderstandingRouter(ocr)
    screen_ref = ContextScreenRefPayload(
        request_id="req1",
        turn_id="t1",
        status="ok",
        artifact_id="art_1",
        target_app="VS Code",
        target_title="main.py",
    )

    # Path A: Vision direct
    vision_profile = ProfileConfig(
        name="flash", label="Flash", provider="openai_compatible", model="deepseek-flash", supports_vision=True
    )
    msg_a, path_a = await router.prepare_user_message(
        user_text="看看这个报错",
        image_bytes=b"fakejpeg",
        content_type="image/jpeg",
        profile=vision_profile,
        screen_ref=screen_ref,
    )
    assert path_a == "vision"
    assert isinstance(msg_a, list)
    assert msg_a[1]["type"] == "image_url"

    # Path B: DeepSeek OCR fallback
    text_profile = ProfileConfig(
        name="chat", label="Chat", provider="openai_compatible", model="deepseek-chat", supports_vision=False
    )
    msg_b, path_b = await router.prepare_user_message(
        user_text="看看这个报错",
        image_bytes=b"fakejpeg",
        content_type="image/jpeg",
        profile=text_profile,
        screen_ref=screen_ref,
    )
    assert path_b == "ocr"
    assert isinstance(msg_b, str)
    assert "ZeroDivisionError: division by zero" in msg_b


class CaptureSpyRouter(LLMRouter):
    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self.seen_user_contents: List[Any] = []
        self.cancelled_event = asyncio.Event()

    async def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        profile_name: str | None = None,
        *,
        json_mode: bool = False,
    ) -> AsyncIterator[str]:
        self.seen_user_contents.append(messages[-1]["content"])
        user_raw = messages[-1]["content"]
        text_str = str(user_raw)
        if "slow_screen_cancel" in text_str:
            try:
                for i in range(60):
                    await asyncio.sleep(0.02)
                    yield f"chunk{i} "
            except asyncio.CancelledError:
                self.cancelled_event.set()
                raise
        yield "[EMOTION:happy]\n看到画面里的 `SyntaxError` 啦，少了一个冒号。"


class StubOCR(OCRProvider):
    async def extract_text(self, image_bytes: bytes, content_type: str = "image/jpeg") -> str:
        return "SyntaxError: expected ':' at line 12"


def _make_phase2_app(tmp_path: Path, *, timeout_sec: float = 1.5):
    cfg = AppConfig(
        default_profile="vision_mock",
        profiles={
            "vision_mock": ProfileConfig(
                name="vision_mock", label="Vision", provider="mock", model="deepseek-flash", supports_vision=True
            ),
            "text_mock": ProfileConfig(
                name="text_mock", label="Text", provider="mock", model="deepseek-chat", supports_vision=False
            ),
        },
        privacy=PrivacyConfig(default_mode="meta_only"),
    )
    cfg.memory.sqlite_path = ":memory:"
    cfg.memory.extraction.enabled = False
    router = CaptureSpyRouter(cfg)
    artifacts = ArtifactStore(base_dir=tmp_path)
    screen_router = ScreenUnderstandingRouter(StubOCR())
    app = create_app(
        config=cfg,
        llm_router=router,
        artifact_store=artifacts,
        screen_router=screen_router,
        capture_timeout_seconds=timeout_sec,
    )
    return app, router, artifacts


def test_screen_capture_permission_guide_offline_timeout_and_burn(tmp_path: Path) -> None:
    app, router, artifacts = _make_phase2_app(tmp_path, timeout_sec=0.3)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as body_ws:
            body_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "b1",
                        "ts": 1,
                        "source": "body",
                        "type": "system.hello",
                        "payload": {"client_name": "body", "role": "body"},
                    }
                )
            )
            assert json.loads(body_ws.receive_text())["type"] == "system.welcome"
            priv = json.loads(body_ws.receive_text())
            assert priv["type"] == "system.privacy_state"
            assert priv["payload"]["mode"] == "meta_only"

            # 1. Under meta_only, asking about screen politely guides without calling LLM
            body_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "b2",
                        "turn_id": "t_meta_deny",
                        "ts": 2,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "帮我看看屏幕上这个报错"},
                    }
                )
            )
            txt = []
            while True:
                m = json.loads(body_ws.receive_text())
                if m["type"] == "output.text_delta":
                    txt.append(m["payload"]["delta"])
                if m["type"] == "avatar.state" and m["payload"]["activity"] == "idle":
                    break
            assert "按需看屏" in "".join(txt)
            assert len(router.seen_user_contents) == 0

            # 2. Switch to full_on_demand while senses is offline -> graceful degradation
            body_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "b3",
                        "ts": 3,
                        "source": "body",
                        "type": "system.privacy_set",
                        "payload": {"mode": "full_on_demand"},
                    }
                )
            )
            assert json.loads(body_ws.receive_text())["payload"]["mode"] == "full_on_demand"

            body_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "b4",
                        "turn_id": "t_offline",
                        "ts": 4,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "帮我看看屏幕上这个报错"},
                    }
                )
            )
            txt = []
            while True:
                m = json.loads(body_ws.receive_text())
                if m["type"] == "output.text_delta":
                    txt.append(m["payload"]["delta"])
                if m["type"] == "avatar.state" and m["payload"]["activity"] == "idle":
                    break
            assert "感官服务尚未连接" in "".join(txt)

            # 3. Connect senses and simulate capture timeout -> graceful recovery
            with client.websocket_connect("/ws") as senses_ws:
                senses_ws.send_text(
                    json.dumps(
                        {
                            "v": "1.0",
                            "id": "s1",
                            "ts": 5,
                            "source": "senses",
                            "type": "system.hello",
                            "payload": {"client_name": "test-senses", "role": "senses", "platform": "macos"},
                        }
                    )
                )
                assert json.loads(senses_ws.receive_text())["type"] == "system.welcome"
                assert json.loads(senses_ws.receive_text())["type"] == "system.privacy_state"
                assert json.loads(body_ws.receive_text())["payload"]["senses_connected"] is True

                # Trigger timeout (senses doesn't respond to capture_request)
                body_ws.send_text(
                    json.dumps(
                        {
                            "v": "1.0",
                            "id": "b5",
                            "turn_id": "t_timeout",
                            "ts": 6,
                            "source": "body",
                            "type": "input.text",
                            "payload": {"text": "看看屏幕报错"},
                        }
                    )
                )
                cap_req = json.loads(senses_ws.receive_text())
                assert cap_req["type"] == "senses.capture_request"
                txt = []
                while True:
                    m = json.loads(body_ws.receive_text())
                    if m["type"] == "output.text_delta":
                        txt.append(m["payload"]["delta"])
                    if m["type"] == "avatar.state" and m["payload"]["activity"] == "idle":
                        break
                assert "超时" in "".join(txt)

                # 4. Upload artifact, respond context.screen_ref, verify burn-after-reading & turn_id cancel
                up = client.post(
                    "/api/artifacts",
                    files={"file": ("shot.jpg", b"jpeg-payload", "image/jpeg")},
                )
                art_id = up.json()["artifact_id"]
                assert artifacts.get_path(art_id) is not None

                body_ws.send_text(
                    json.dumps(
                        {
                            "v": "1.0",
                            "id": "b6",
                            "turn_id": "t_cancel_screen",
                            "ts": 7,
                            "source": "body",
                            "type": "input.action",
                            "payload": {
                                "action": "ask_with_screen",
                                "data": {"text": "slow_screen_cancel"},
                            },
                        }
                    )
                )
                cap_req2 = json.loads(senses_ws.receive_text())
                senses_ws.send_text(
                    json.dumps(
                        {
                            "v": "1.0",
                            "id": "s2",
                            "turn_id": "t_cancel_screen",
                            "ts": 8,
                            "source": "senses",
                            "type": "context.screen_ref",
                            "payload": {
                                "request_id": cap_req2["payload"]["request_id"],
                                "turn_id": "t_cancel_screen",
                                "status": "ok",
                                "artifact_id": art_id,
                                "width": 1280,
                                "height": 720,
                                "target_app": "VS Code",
                                "target_title": "main.py",
                            },
                        }
                    )
                )
                # Wait for first delta, then cancel
                while True:
                    m = json.loads(body_ws.receive_text())
                    if m["type"] == "output.text_delta":
                        break
                body_ws.send_text(
                    json.dumps(
                        {
                            "v": "1.0",
                            "id": "b7",
                            "turn_id": "t_cancel_screen",
                            "ts": 9,
                            "source": "body",
                            "type": "input.cancel",
                            "payload": {"reason": "user_stopped"},
                        }
                    )
                )
                while True:
                    m = json.loads(body_ws.receive_text())
                    if m["type"] == "avatar.state" and m["payload"]["activity"] == "idle":
                        break

                assert router.cancelled_event.is_set()
                # Physical screenshot file MUST be burned immediately in finally even after cancel
                assert artifacts.get_path(art_id) is None
                assert len(list(tmp_path.glob("*"))) == 0

                # 5. Clear history action keeps privacy state intact
                body_ws.send_text(
                    json.dumps(
                        {
                            "v": "1.0",
                            "id": "b8",
                            "ts": 10,
                            "source": "body",
                            "type": "input.action",
                            "payload": {"action": "clear_history"},
                        }
                    )
                )
                cleared_msg = json.loads(body_ws.receive_text())
                assert cleared_msg["type"] == "output.tool_status"
                assert cleared_msg["payload"]["tool"] == "memory"
