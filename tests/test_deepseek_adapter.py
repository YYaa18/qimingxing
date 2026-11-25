from __future__ import annotations

import json
from typing import Any, Dict, List

import httpx
import pytest

from brain.config import AppConfig, ProfileConfig
from brain.hub import build_turn_tools_schema
from brain.llm_router import LLMError, LLMRouter, estimate_cny_cost


def _cfg(*, thinking: str = "auto", effort: str = "", model: str = "deepseek-flash") -> AppConfig:
    cfg = AppConfig(
        default_profile="ds",
        profiles={
            "ds": ProfileConfig(
                name="ds",
                label="DeepSeek",
                provider="openai_compatible",
                provider_id="deepseek",
                model=model,
                base_url="https://api.deepseek.com/v1",
                api_key="sk-test",
            )
        },
    )
    cfg.deepseek.thinking = thinking
    cfg.deepseek.reasoning_effort = effort
    return cfg


def _sse(chunks: List[Dict[str, Any]]) -> str:
    return "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def _stream_response(chunks: List[Dict[str, Any]]) -> httpx.Response:
    return httpx.Response(
        200,
        text=_sse(chunks),
        headers={"content-type": "text/event-stream"},
    )


@pytest.mark.asyncio
async def test_deepseek_thinking_and_latest_reminder_body() -> None:
    captured: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _stream_response([
            {"choices": [{"delta": {"reasoning_content": "先想一想"}}]},
            {"choices": [{"delta": {"content": "好"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
        ])

    cfg = _cfg(thinking="enabled", effort="low")
    cfg.deepseek.safety_check = True
    router = LLMRouter(cfg, transport=httpx.MockTransport(handler))
    out = "".join([tok async for tok in router.stream_chat(
        [{"role": "user", "content": "你好"}], profile_name="ds"
    )])

    assert out == "好"
    assert captured["thinking"] == {"type": "enabled"}
    assert captured["reasoning_effort"] == "low"
    assert "temperature" not in captured
    assert captured["safety_check"] == {"level": "high"}
    assert captured["user_id"] == "qimingxing-desktop"
    messages = captured["messages"]
    assert messages[-2]["role"] == "latest_reminder"
    assert messages[-1] == {"role": "user", "content": "你好"}
    assert "App" in messages[-2]["content"]
    assert "中文" in messages[-2]["content"]


@pytest.mark.asyncio
async def test_deepseek_disabled_thinking_keeps_temperature() -> None:
    captured: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _stream_response([
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
        ])

    cfg = _cfg(thinking="disabled", effort="high")
    router = LLMRouter(cfg, transport=httpx.MockTransport(handler))
    _ = "".join([tok async for tok in router.stream_chat(
        [{"role": "user", "content": "hi"}], profile_name="ds"
    )])

    assert captured["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in captured
    assert "temperature" in captured


@pytest.mark.asyncio
async def test_deepseek_json_mode_is_fast_and_has_no_reminder() -> None:
    captured: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _stream_response([
            {"choices": [{"delta": {"content": '{"ok":true}'}, "finish_reason": "stop"}]},
        ])

    cfg = _cfg(thinking="enabled", effort="max")
    router = LLMRouter(cfg, transport=httpx.MockTransport(handler))
    _ = "".join([tok async for tok in router.stream_chat(
        [{"role": "system", "content": "json"}, {"role": "user", "content": "extract"}],
        profile_name="ds",
        json_mode=True,
    )])

    assert captured["thinking"] == {"type": "disabled"}
    assert captured["response_format"] == {"type": "json_object"}
    assert all(m["role"] != "latest_reminder" for m in captured["messages"])


@pytest.mark.asyncio
async def test_deepseek_tool_round_captures_reasoning_and_citations() -> None:
    events: List[Dict[str, Any]] = []
    captured: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _stream_response([
            {"choices": [{"delta": {}, "server_tool_event": {
                "type": "call_started",
                "call": {"kind": "search", "status": "running"},
            }}]},
            {"choices": [{"delta": {"reasoning_content": "需要查一下"}}]},
            {"choices": [{"delta": {"content": "答案"}}]},
            {"choices": [{"delta": {
                "annotations": [{
                    "type": "url_citation",
                    "url_citation": {
                        "url": "https://example.com/a",
                        "title": "Example source",
                        "start_index": 0,
                        "end_index": 2,
                    },
                }],
            }, "finish_reason": "stop"}]},
            {"choices": [], "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 8,
                "total_tokens": 28,
                "completion_tokens_details": {"reasoning_tokens": 5},
                "prompt_cache_hit_tokens": 12,
            }},
        ])

    cfg = _cfg(thinking="enabled")
    router = LLMRouter(cfg, transport=httpx.MockTransport(handler))

    async def on_event(event: Dict[str, Any]) -> None:
        events.append(event)

    deltas: List[str] = []

    async def on_delta(text: str) -> None:
        deltas.append(text)

    res = await router.complete_with_tools(
        [{"role": "user", "content": "搜索一下"}],
        [{"type": "deepseek_web_search"}, {"type": "deepseek_read_url"}],
        profile_name="ds",
        on_text_delta=on_delta,
        on_server_tool_event=on_event,
    )

    assert captured["tools"] == [
        {"type": "deepseek_web_search"},
        {"type": "deepseek_read_url"},
    ]
    assert res.reasoning_content == "需要查一下"
    assert res.annotations[0]["url_citation"]["url"] == "https://example.com/a"
    assert "参考来源" in res.content
    assert "".join(deltas) == res.content
    assert events and events[0]["type"] == "call_started"
    assert res.usage.reasoning_tokens == 5
    assert res.usage.prompt_cache_hit_tokens == 12


@pytest.mark.asyncio
async def test_deepseek_http_errors_are_actionable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    cfg = _cfg()
    router = LLMRouter(cfg, transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError) as excinfo:
        _ = "".join([tok async for tok in router.stream_chat(
            [{"role": "user", "content": "hi"}], profile_name="ds"
        )])
    assert "429" in excinfo.value.message
    assert "速率" in excinfo.value.message


@pytest.mark.asyncio
async def test_non_deepseek_body_keeps_generic_openai_shape() -> None:
    captured: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _stream_response([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}])

    cfg = AppConfig(
        default_profile="other",
        profiles={
            "other": ProfileConfig(
                name="other",
                label="Other",
                provider="openai_compatible",
                provider_id="openai",
                model="gpt-4o",
                base_url="https://api.openai.com/v1",
                api_key="sk-test",
            )
        },
    )
    router = LLMRouter(cfg, transport=httpx.MockTransport(handler))
    _ = "".join([tok async for tok in router.stream_chat(
        [{"role": "user", "content": "hi"}], profile_name="other"
    )])
    assert "thinking" not in captured
    assert "reasoning_effort" not in captured
    assert "temperature" in captured
    assert all(m["role"] != "latest_reminder" for m in captured["messages"])


class _FakeRegistry:
    def to_openai_tools(self):
        return [
            {"type": "function", "function": {"name": "web_search"}},
            {"type": "function", "function": {"name": "web_fetch"}},
            {"type": "function", "function": {"name": "shell_exec"}},
        ]


def test_native_web_tools_replace_client_search_only_for_flash() -> None:
    cfg = AppConfig()
    flash = ProfileConfig(
        name="ds", label="DS", provider="openai_compatible",
        provider_id="deepseek", model="deepseek-flash",
        base_url="https://api.deepseek.com/v1", api_key="k",
    )
    schema, native = build_turn_tools_schema(
        _FakeRegistry(), cfg, flash, use_tools=True
    )
    assert native is True
    assert {"type": "deepseek_web_search"} in schema
    assert {"type": "deepseek_read_url"} in schema
    assert not any(
        (tool.get("function") or {}).get("name") in ("web_search", "web_fetch")
        for tool in schema
    )
    assert any(
        (tool.get("function") or {}).get("name") == "shell_exec"
        for tool in schema
    )

    pro = ProfileConfig(
        name="pro", label="Pro", provider="openai_compatible",
        provider_id="deepseek", model="deepseek-v4-pro",
        base_url="https://api.deepseek.com/v1", api_key="k",
    )
    schema_pro, native_pro = build_turn_tools_schema(
        _FakeRegistry(), cfg, pro, use_tools=True
    )
    assert native_pro is False
    assert any(
        (tool.get("function") or {}).get("name") == "web_search"
        for tool in schema_pro
    )


def test_deepseek_peak_pricing_defaults() -> None:
    assert estimate_cny_cost("deepseek-flash", 1_000_000, 0, 0) == 2.0
    assert estimate_cny_cost("deepseek-flash", 1_000_000, 1_000_000, 0) == 0.04
    assert estimate_cny_cost("deepseek-v4-pro", 1_000_000, 0, 0) == 9.0
    assert estimate_cny_cost("deepseek-v4-pro", 1_000_000, 0, 1_000_000) == 36.0
