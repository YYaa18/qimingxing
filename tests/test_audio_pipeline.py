from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

import httpx
from fastapi.testclient import TestClient

from brain.audio import (
    ASRProvider,
    ASRResult,
    DeepSeekASRProvider,
    DeepSeekTTSProvider,
    SentenceChunker,
    TTSProvider,
)
from brain.config import (
    AppConfig,
    AudioASRConfig,
    AudioConfig,
    AudioTTSConfig,
    ProfileConfig,
)
from brain.llm_router import LLMCompletionResult, LLMRouter, LLMUsage
from brain.main import create_app


def test_sentence_chunker_splits_clauses_and_silences_code_and_cards() -> None:
    chunker = SentenceChunker(min_chars=4, max_chars=80)
    stream = [
        "[EMO",
        "TION:happy]\n你好呀，这是第一句。",
        "接下来给你看代码：",
        "``",
        "`python\nprint('secret-code-do-not-read')\n``",
        "`\n",
        "[CARD:spoiler_steps|提示]卡片内容也不应该朗读[/CARD]",
        "还有最后一句收尾！",
    ]
    out: List[str] = []
    for delta in stream:
        out.extend(chunker.feed(delta))
    out.extend(chunker.flush())

    joined = " ".join(out)
    assert any("这是第一句" in s for s in out)
    assert any("最后一句收尾" in s for s in out)
    assert "secret-code-do-not-read" not in joined
    assert "EMOTION" not in joined
    assert "卡片内容也不应该朗读" not in joined


async def test_deepseek_asr_and_tts_providers_mock_transport() -> None:
    seen_asr: Dict[str, Any] = {}
    seen_tts: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tools/asr"):
            seen_asr["auth"] = request.headers.get("Authorization")
            seen_asr["content"] = request.content
            return httpx.Response(
                200,
                json={"request_id": "req_1", "text": "你好启明星", "duration_ms": 1200},
            )
        if request.url.path.endswith("/tools/tts"):
            seen_tts["auth"] = request.headers.get("Authorization")
            seen_tts["json"] = json.loads(request.content.decode("utf-8"))

            class _ChunkedStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    # Intentionally emit an odd-length chunk followed by 1 byte
                    yield b"\x01\x02\x03"
                    yield b"\x04\x05\x06"

            return httpx.Response(200, stream=_ChunkedStream())
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    asr = DeepSeekASRProvider(
        AudioASRConfig(api_key="sk-test-asr-key", base_url="https://api.deepseek.com/v1"),
        transport=transport,
    )
    pcm_in = b"\x00\x01" * 1600
    res = await asr.transcribe_pcm(pcm_in, sample_rate=16000, channels=1)
    assert res.text == "你好启明星"
    assert res.duration_ms == 1200
    assert res.estimated_cost_cny > 0
    assert seen_asr["auth"] == "Bearer sk-test-asr-key"
    assert b"RIFF" in seen_asr["content"] and b"WAVE" in seen_asr["content"]

    tts = DeepSeekTTSProvider(
        AudioTTSConfig(api_key="sk-test-tts-key", base_url="https://api.deepseek.com/v1", voice="stella"),
        transport=transport,
    )
    chunks = [c async for c in tts.synthesize_pcm_stream("测试朗读")]
    combined = b"".join(chunks)
    assert all(len(c) % 2 == 0 for c in chunks)
    assert combined == b"\x01\x02\x03\x04\x05\x06"
    assert seen_tts["json"]["voice_id"] == "stella"
    assert seen_tts["json"]["format"] == "pcm"

    # Providers keep one HTTP client instead of a fresh TLS handshake per call.
    asr_client = asr._client
    await asr.transcribe_pcm(pcm_in, sample_rate=16000, channels=1)
    assert asr._client is asr_client
    tts_client = tts._client
    _ = [c async for c in tts.synthesize_pcm_stream("第二句") ]
    assert tts._client is tts_client
    await asr.aclose()
    await tts.aclose()


class _FakeASR(ASRProvider):
    def __init__(self, reply_text: str = "今天北京天气怎么样？") -> None:
        self.reply_text = reply_text
        self.calls = 0

    async def transcribe_pcm(
        self, pcm_bytes: bytes, *, sample_rate: int = 16000, channels: int = 1
    ) -> ASRResult:
        self.calls += 1
        return ASRResult(
            text=self.reply_text,
            duration_ms=800,
            request_id="fake-asr-1",
            estimated_cost_cny=0.002,
        )


class _FakeTTS(TTSProvider):
    def __init__(self, *, slow: bool = False, fail: bool = False) -> None:
        self.slow = slow
        self.fail = fail
        self.sentences: List[str] = []

    async def synthesize_pcm_stream(
        self, text: str, *, voice: Optional[str] = None
    ) -> AsyncIterator[bytes]:
        self.sentences.append(text)
        if self.fail:
            raise RuntimeError("TTS backend error")
        yield b"\x10\x00\x20\x00\x30\x00\x40\x00"
        if self.slow:
            await asyncio.sleep(0.4)
        yield b"\x50\x00\x60\x00"


class _FakeVoiceRouter(LLMRouter):
    def __init__(self, config: AppConfig, scripted_replies: List[str]) -> None:
        super().__init__(config)
        self._replies = list(scripted_replies)

    async def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        profile_name: Optional[str] = None,
        json_mode: bool = False,
    ) -> AsyncIterator[str]:
        reply = self._replies.pop(0) if self._replies else "好的。"
        self.last_usage = LLMUsage(
            profile=profile_name or self.active_profile,
            model="mock-v",
            prompt_tokens=20,
            completion_tokens=10,
            total_tokens=30,
            estimated_cost_cny=0.001,
        )
        yield reply


def test_hub_voice_loop_barge_in_usage_ledger_and_godot_capture_hide(tmp_path: Path) -> None:
    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="m")},
        audio=AudioConfig(enabled=True),
    )
    cfg.memory.sqlite_path = str(tmp_path / "voice_hub.db")
    cfg.memory.extraction.enabled = False
    cfg.tools.enabled = False
    cfg.privacy.default_mode = "full_on_demand"

    asr = _FakeASR("请简要问候我一下。")
    tts = _FakeTTS(slow=False, fail=False)
    router = _FakeVoiceRouter(
        cfg,
        [
            "[EMOTION:happy]\n你好呀，我是启明星！请问有什么可以帮你的？",
            "[EMOTION:happy]\n这是一段会持续很久的朗读。第二句话也准备开始播报！",
        ],
    )

    app = create_app(config=cfg, llm_router=router, asr_provider=asr, tts_provider=tts)

    def _recv_until(ws, expected_type: str) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        seen: List[Dict[str, Any]] = []
        while True:
            msg = json.loads(ws.receive_text())
            seen.append(msg)
            if msg["type"] == expected_type:
                return msg, seen

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as body_ws, client.websocket_connect("/ws") as senses_ws:
            # 1. Godot body hello using string list capabilities
            body_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "hb",
                        "ts": 1,
                        "source": "body",
                        "type": "system.hello",
                        "payload": {
                            "client_name": "body_godot",
                            "role": "body",
                            "client_type": "body_godot",
                            "capabilities": ["audio_out", "pin_image", "auto_hide"],
                        },
                    }
                )
            )
            _recv_until(body_ws, "system.welcome")
            _recv_until(body_ws, "system.privacy_state")

            # 2. Senses hello
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
            _recv_until(senses_ws, "system.welcome")
            _recv_until(senses_ws, "system.privacy_state")
            _recv_until(body_ws, "system.privacy_state")

            # 3. Full PTT voice turn via senses -> Brain ASR -> LLM -> streaming TTS -> Godot
            turn1 = "voice_turn_1"
            senses_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "a1",
                        "turn_id": turn1,
                        "ts": 3,
                        "source": "senses",
                        "type": "audio.input_start",
                        "payload": {"turn_id": turn1, "sample_rate": 16000, "channels": 1},
                    }
                )
            )
            senses_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "a2",
                        "turn_id": turn1,
                        "ts": 4,
                        "source": "senses",
                        "type": "audio.input_chunk",
                        "payload": {
                            "turn_id": turn1,
                            "seq": 1,
                            "data_b64": base64.b64encode(b"\x00\x01" * 800).decode("ascii"),
                        },
                    }
                )
            )
            senses_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "a3",
                        "turn_id": turn1,
                        "ts": 5,
                        "source": "senses",
                        "type": "audio.input_end",
                        "payload": {"turn_id": turn1, "reason": "ptt_released"},
                    }
                )
            )

            end_msg, turn1_msgs = _recv_until(body_ws, "output.turn_end")
            assert end_msg["payload"]["finish_reason"] == "completed"
            msg_types = [m["type"] for m in turn1_msgs]
            assert "audio.output_start" in msg_types
            assert "audio.output_chunk" in msg_types
            if "audio.output_end" not in msg_types:
                # turn_end is sent before waiting for TTS so the shell unlocks
                # immediately; audio may finish afterwards.
                _, audio_msgs = _recv_until(body_ws, "audio.output_end")
                msg_types.extend(m["type"] for m in audio_msgs)
                assert audio_msgs[-1]["type"] == "audio.output_end"
            assert "audio.output_end" in msg_types
            assert len(tts.sentences) >= 2
            assert asr.calls == 1

            dbg = client.get("/api/memory/debug").json()
            assert dbg["today_usage"]["calls"] >= 3  # ASR + LLM + TTS
            assert dbg["today_usage"]["total_cost_cny"] > 0

            # 4. Barge-in test: slow TTS interrupted by senses input.cancel
            tts.slow = True
            body_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "q2",
                        "turn_id": "voice_turn_2",
                        "ts": 6,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "长回答测试"},
                    }
                )
            )
            _recv_until(body_ws, "audio.output_chunk")
            senses_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "c1",
                        "ts": 7,
                        "source": "senses",
                        "type": "input.cancel",
                        "payload": {"reason": "ptt_barge_in"},
                    }
                )
            )
            # turn_end is sent before TTS finishes, so the text turn may already
            # be complete when barge-in arrives; cancellation then applies to
            # the remaining audio. Accept either ordering for the text turn.
            cancel_audio_end, cancel_msgs = _recv_until(body_ws, "audio.output_end")
            assert cancel_audio_end["payload"]["finish_reason"] == "cancelled"
            turn_ends = [m for m in cancel_msgs if m["type"] == "output.turn_end"]
            assert all(
                m["payload"]["finish_reason"] in ("completed", "cancelled")
                for m in turn_ends
            )

            # 5. Godot auto-hide handshake during screen capture
            body_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "cap1",
                        "turn_id": "cap_turn",
                        "ts": 8,
                        "source": "body",
                        "type": "input.action",
                        "payload": {"action": "ask_with_screen", "data": {"text": "帮我看看屏幕"}},
                    }
                )
            )
            prep_msg, _ = _recv_until(body_ws, "system.capture_prepare")
            assert prep_msg["payload"]["hide_ms"] == 150
            cap_req, _ = _recv_until(senses_ws, "senses.capture_request")
            senses_ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "cr1",
                        "turn_id": "cap_turn",
                        "ts": 9,
                        "source": "senses",
                        "type": "context.screen_ref",
                        "payload": {
                            "request_id": cap_req["payload"]["request_id"],
                            "turn_id": "cap_turn",
                            "status": "privacy_blocked",
                        },
                    }
                )
            )
            done_msg, _ = _recv_until(body_ws, "system.capture_done")
            assert done_msg["payload"]["request_id"] == prep_msg["payload"]["request_id"]


def test_tts_failure_degrades_gracefully_without_breaking_turn(tmp_path: Path) -> None:
    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="m")},
        audio=AudioConfig(enabled=True),
    )
    cfg.memory.sqlite_path = str(tmp_path / "voice_fail.db")
    cfg.memory.extraction.enabled = False
    cfg.tools.enabled = False

    router = _FakeVoiceRouter(cfg, ["语音合成坏了也要正常出字。"])
    app = create_app(config=cfg, llm_router=router, tts_provider=_FakeTTS(fail=True))

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
                        "payload": {
                            "client_name": "body_godot",
                            "role": "body",
                            "client_type": "body_godot",
                            "capabilities": ["audio_out"],
                        },
                    }
                )
            )
            while json.loads(ws.receive_text())["type"] != "system.privacy_state":
                pass

            ws.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "q",
                        "turn_id": "t_tts_fail",
                        "ts": 2,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "你好"},
                    }
                )
            )
            texts: List[str] = []
            while True:
                msg = json.loads(ws.receive_text())
                if msg["type"] == "output.text_delta":
                    texts.append(msg["payload"]["delta"])
                if msg["type"] == "output.turn_end":
                    assert msg["payload"]["finish_reason"] == "completed"
                    break
            assert "语音合成坏了也要正常出字。" in "".join(texts)


def test_sentence_chunker_silences_equals_emotion_tag() -> None:
    chunker = SentenceChunker(min_chars=2, max_chars=80)
    out = list(chunker.feed("[EMOTION=happy]\n工作区开好了。"))
    out.extend(chunker.flush())
    joined = " ".join(out)
    assert "工作区开好了" in joined
    assert "EMOTION" not in joined
