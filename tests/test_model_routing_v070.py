from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict

import httpx
import yaml
from fastapi.testclient import TestClient

from brain.audio import RoutedASRProvider, RoutedTTSProvider
from brain.config import load_config, save_settings_to_yaml
from brain.llm_router import LLMRouter
from brain.main import create_app
from brain.memory.models import UsageLedgerRecord
from brain.memory.sqlite_store import SQLiteMemoryStore
from brain.screen_router import DeepSeekOCRProvider, ScreenUnderstandingRouter
from protocol.models import ContextScreenRefPayload


def test_legacy_config_compat_and_routing_secret_preservation(tmp_path: Path) -> None:
    cfg_path = tmp_path / "legacy.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "default_profile": "deepseek_flash",
                "profiles": {
                    "deepseek_flash": {
                        "label": "DeepSeek Flash",
                        "provider": "openai_compatible",
                        "model": "deepseek-flash",
                        "base_url": "https://api.deepseek.com/v1",
                        "api_key": "sk-ds-secret-1111",
                        "supports_vision": True,
                    }
                },
                "ocr": {
                    "provider": "deepseek_ocr",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "sk-ds-secret-1111",
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)
    assert cfg.has_explicit_routing is False
    assert cfg.providers["deepseek"].api_key == "sk-ds-secret-1111"
    assert cfg.providers["local_ollama"].auth_required is False
    assert cfg.model_routing.chat.provider == "deepseek"
    assert cfg.model_routing.chat.model == "deepseek-flash"

    # Update siliconflow key + assign 5-slot routing, passing masked deepseek key
    updated = save_settings_to_yaml(
        {
            "providers": {
                "deepseek": {"api_key": "sk-d****1111"},
                "siliconflow": {
                    "label": "硅基流动",
                    "base_url": "https://api.siliconflow.cn/v1",
                    "api_key": "sk-sf-secret-2222",
                },
            },
            "model_routing": {
                "chat": {"provider": "deepseek", "model": "deepseek-chat"},
                "vision_ocr": {"provider": "siliconflow", "model": "Qwen/Qwen2.5-VL-72B-Instruct"},
                "fast_utility": {"provider": "local_ollama", "model": "qwen2.5:7b"},
                "asr": {"provider": "siliconflow", "model": "FunAudioLLM/SenseVoiceSmall"},
                "tts": {"provider": "deepseek", "model": "deepseek-tts", "voice": "tide"},
            },
        },
        config_path=cfg_path,
    )

    assert (os.stat(cfg_path).st_mode & 0o777) == 0o600
    assert updated.has_explicit_routing is True
    assert updated.providers["deepseek"].api_key == "sk-ds-secret-1111"
    assert updated.providers["siliconflow"].api_key == "sk-sf-secret-2222"
    assert updated.default_profile == "routed_chat"
    assert updated.model_routing.fast_utility.provider == "local_ollama"
    assert updated.audio.asr.provider == "siliconflow"
    assert updated.audio.tts.voice == "tide"


async def test_five_slot_routing_endpoints_and_usage_ledger_migration(tmp_path: Path) -> None:
    # 1. Create an older SQLite DB missing `provider` and `slot` columns in usage_ledger
    db_path = tmp_path / "legacy_usage.db"
    raw_conn = sqlite3.connect(str(db_path))
    raw_conn.execute(
        """
        CREATE TABLE usage_ledger (
            ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT '',
            turn_id TEXT NOT NULL DEFAULT '',
            profile TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            prompt_cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost_cny REAL NOT NULL DEFAULT 0.0,
            created_at REAL NOT NULL
        )
        """
    )
    raw_conn.commit()
    raw_conn.close()

    store = SQLiteMemoryStore(db_path)
    await store.initialize()

    # 2. Configure 5 independent slots across DeepSeek, SiliconFlow, and keyless local Ollama
    cfg_path = tmp_path / "routing.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "deepseek": {
                        "label": "DeepSeek",
                        "protocol": "openai_compatible",
                        "base_url": "https://api.deepseek.com/v1",
                        "api_key": "sk-ds-1111",
                    },
                    "siliconflow": {
                        "label": "硅基流动",
                        "protocol": "openai_compatible",
                        "base_url": "https://api.siliconflow.cn/v1",
                        "api_key": "sk-sf-2222",
                    },
                    "local_ollama": {
                        "label": "本地 Ollama",
                        "protocol": "openai_compatible",
                        "base_url": "http://127.0.0.1:11434/v1",
                        "auth_required": False,
                    },
                },
                "model_routing": {
                    "chat": {"provider": "deepseek", "model": "deepseek-chat"},
                    "vision_ocr": {"provider": "siliconflow", "model": "Qwen/Qwen2.5-VL-72B-Instruct"},
                    "fast_utility": {"provider": "local_ollama", "model": "qwen2.5:7b-custom"},
                    "asr": {"provider": "siliconflow", "model": "FunAudioLLM/SenseVoiceSmall"},
                    "tts": {"provider": "siliconflow", "model": "FunAudioLLM/CosyVoice2-0.5B", "voice": "alex"},
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)

    captured_requests: list[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        auth = request.headers.get("Authorization", "")
        if url_str.endswith("/chat/completions"):
            body = json.loads(request.content.decode("utf-8"))
            captured_requests.append({"url": url_str, "auth": auth, "model": body.get("model")})
            if body.get("stream"):
                sse = (
                    'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n'
                    'data: {"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15},"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                    "data: [DONE]\n\n"
                ).encode("utf-8")
                return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "屏幕截图解析完成"}}],
                    "usage": {"prompt_tokens":20, "completion_tokens":8, "total_tokens":28},
                },
            )
        if url_str.endswith("/audio/transcriptions"):
            captured_requests.append({"url": url_str, "auth": auth, "kind": "asr"})
            return httpx.Response(200, json={"text": "语音识别成功", "duration_ms": 900})
        if url_str.endswith("/audio/speech"):
            body = json.loads(request.content.decode("utf-8"))
            captured_requests.append({"url": url_str, "auth": auth, "model": body.get("model"), "voice": body.get("voice")})
            return httpx.Response(200, content=b"\x01\x02\x03\x04")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    router = LLMRouter(cfg, transport=transport)

    # Chat slot
    chat_chunks = [c async for c in router.stream_chat([{"role": "user", "content": "hi"}])]
    assert "".join(chat_chunks) == "OK"
    assert router.last_usage is not None
    assert router.last_usage.provider == "deepseek"
    assert router.last_usage.slot == "chat"
    await store.add_usage_record(
        UsageLedgerRecord(
            session_id="s1",
            turn_id="t1",
            profile=router.last_usage.profile,
            provider=router.last_usage.provider,
            slot=router.last_usage.slot,
            model=router.last_usage.model,
            prompt_tokens=router.last_usage.prompt_tokens,
            completion_tokens=router.last_usage.completion_tokens,
            estimated_cost_cny=0.001,
        )
    )

    # Fast utility slot (keyless Ollama custom model)
    fast_res = await router.complete_with_tools(
        [{"role": "user", "content": "summarize"}], [], profile_name="@slot:fast_utility"
    )
    assert fast_res.usage.provider == "local_ollama"
    assert fast_res.usage.slot == "fast_utility"
    assert fast_res.usage.model == "qwen2.5:7b-custom"
    await store.add_usage_record(
        UsageLedgerRecord(
            session_id="s1",
            turn_id="t1",
            profile=fast_res.usage.profile,
            provider=fast_res.usage.provider,
            slot=fast_res.usage.slot,
            model=fast_res.usage.model,
            prompt_tokens=fast_res.usage.prompt_tokens,
            completion_tokens=fast_res.usage.completion_tokens,
            estimated_cost_cny=0.0005,
        )
    )

    # Vision/OCR slot (SiliconFlow VLM)
    sr = ScreenUnderstandingRouter(DeepSeekOCRProvider(cfg.ocr, cfg), config=cfg, transport=transport)
    prepared, mode = await sr.prepare_user_message(
        user_text="看下画面",
        image_bytes=b"\xff\xd8\xff\xe0test",
        content_type="image/jpeg",
        profile=router.get_profile(),
        screen_ref=ContextScreenRefPayload(request_id="r1", turn_id="t1", status="ok", artifact_id="a1"),
    )
    assert mode == "vision_slot"
    assert "屏幕截图解析完成" in str(prepared)
    assert sr.last_ocr_usage is not None and sr.last_ocr_usage.provider == "siliconflow"
    assert sr.last_ocr_usage.slot == "vision_ocr"

    # ASR & TTS slots (SiliconFlow OpenAI-compatible audio endpoints)
    asr = RoutedASRProvider(cfg.audio.asr, transport=transport)
    asr_res = await asr.transcribe_pcm(b"\x00\x01" * 400)
    assert asr_res.text == "语音识别成功"

    tts = RoutedTTSProvider(cfg.audio.tts, transport=transport)
    pcm_out = b"".join([c async for c in tts.synthesize_pcm_stream("测试声音")])
    assert pcm_out == b"\x01\x02\x03\x04"

    records = await store.list_usage_records(limit=10)
    await store.close()
    assert {(r.provider, r.slot) for r in records} == {
        ("deepseek", "chat"),
        ("local_ollama", "fast_utility"),
    }

    urls = [r["url"] for r in captured_requests]
    assert "https://api.deepseek.com/v1/chat/completions" in urls
    assert "http://127.0.0.1:11434/v1/chat/completions" in urls
    assert "https://api.siliconflow.cn/v1/chat/completions" in urls
    assert "https://api.siliconflow.cn/v1/audio/transcriptions" in urls
    assert "https://api.siliconflow.cn/v1/audio/speech" in urls


def test_provider_connectivity_probe_endpoint_and_redaction(tmp_path: Path) -> None:
    cfg_path = tmp_path / "probe.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "deepseek": {
                        "label": "DeepSeek",
                        "protocol": "openai_compatible",
                        "base_url": "https://api.deepseek.com/v1",
                        "api_key": "sk-probe-valid-8888",
                    }
                },
                "model_routing": {"chat": {"provider": "deepseek", "model": "deepseek-chat"}},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    cfg.memory.sqlite_path = str(tmp_path / "probe.db")
    cfg.memory.extraction.enabled = False

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        if "sk-probe-valid-8888" in auth:
            return httpx.Response(200, json={"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]})
        return httpx.Response(401, text=f"Invalid token sk-bad-secret-9999 in header")

    app = create_app(
        config=cfg,
        settings_config_path=cfg_path,
        probe_transport=httpx.MockTransport(handler),
    )

    with TestClient(app) as client:
        # Masked key resolves saved secret cleanly
        ok_resp = client.post(
            "/api/config/test_provider",
            json={"provider_id": "deepseek", "api_key": "sk-p****8888"},
        )
        assert ok_resp.status_code == 200
        ok_data = ok_resp.json()
        assert ok_data["ok"] is True
        assert ok_data["latency_ms"] >= 1
        assert "deepseek-chat" in ok_data["models"]

        # Invalid explicit key redacts secrets in error message
        bad_resp = client.post(
            "/api/config/test_provider",
            json={
                "provider_id": "custom_test",
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-bad-secret-9999",
            },
        )
        assert bad_resp.status_code == 200
        bad_data = bad_resp.json()
        assert bad_data["ok"] is False
        assert bad_data["error"] == "HTTP_401"
        assert "sk-bad-secret-9999" not in bad_resp.text
