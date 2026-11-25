from __future__ import annotations

import json
from typing import Any, Dict, List

import httpx
import pytest

from brain.config import AppConfig, ProfileConfig
from brain.gemini_native import (
    build_request,
    convert_messages,
    convert_tools,
    grounding_sources,
    citation_footer,
    is_gemini_native_profile,
)
from brain.llm_router import LLMRouter, estimate_cny_cost
from brain.tools.base import Tool, ToolExecutionContext, ToolResult
from brain.tools.executor import ToolExecutor
from brain.tools.registry import ToolRegistry
from protocol.models import RiskLevel


def _cfg() -> AppConfig:
    cfg = AppConfig(
        default_profile="g",
        profiles={
            "g": ProfileConfig(
                name="g",
                label="Gemini",
                provider="openai_compatible",
                provider_id="gemini",
                model="gemini-4-hydrogen",
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                api_key="g-key",
            )
        },
    )
    cfg.gemini.thinking_level = "low"
    return cfg


def _sse(chunks: List[Dict[str, Any]]) -> str:
    return "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks)


def _response(chunks: List[Dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, text=_sse(chunks), headers={"content-type": "text/event-stream"})


def test_native_detection_and_message_conversion() -> None:
    cfg = _cfg()
    profile = cfg.profiles["g"]
    assert is_gemini_native_profile(profile)
    proxy = ProfileConfig(
        name="proxy", label="Proxy", provider="openai_compatible",
        provider_id="gemini", model="gemini-4-hydrogen",
        base_url="https://my-proxy.example.com/v1", api_key="k",
    )
    assert not is_gemini_native_profile(proxy)

    messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "现在几点？"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_time", "arguments": "{}"},
                    "thought_signature": "sig-1",
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "get_date", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"result":"12:00"}'},
        {"role": "tool", "tool_call_id": "call_2", "content": "2026-11-26"},
    ]
    system, contents = convert_messages(messages)
    assert system == "系统提示"
    assert contents[0] == {"role": "user", "parts": [{"text": "现在几点？"}]}
    model_parts = contents[1]["parts"]
    assert model_parts[0]["functionCall"]["name"] == "get_time"
    assert model_parts[0]["thoughtSignature"] == "sig-1"
    assert model_parts[1]["functionCall"]["name"] == "get_date"
    assert "thoughtSignature" not in model_parts[1]
    assert contents[2]["role"] == "user"
    responses = contents[2]["parts"]
    assert len(responses) == 2
    assert responses[0]["functionResponse"]["response"] == {"result": "12:00"}
    assert responses[1]["functionResponse"]["response"] == {"result": "2026-11-26"}


def test_build_request_thinking_json_and_server_tools() -> None:
    cfg = _cfg()
    profile = cfg.profiles["g"]
    cfg.gemini.google_search = True
    cfg.gemini.url_context = True
    body = build_request(
        [{"role": "user", "content": "hi"}],
        [
            {"type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "web_fetch", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "x", "parameters": {"type": "object"}}},
        ],
        profile,
        cfg,
    )
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "LOW"}
    tools = body["tools"]
    declarations = next(tool["functionDeclarations"] for tool in tools if "functionDeclarations" in tool)
    assert [d["name"] for d in declarations] == ["x"]
    assert {"googleSearch": {}} in tools
    assert {"urlContext": {}} in tools
    assert body["toolConfig"]["includeServerSideToolInvocations"] is True

    json_body = build_request(
        [{"role": "system", "content": "json"}, {"role": "user", "content": "extract"}],
        [],
        profile,
        cfg,
        json_mode=True,
    )
    assert json_body["generationConfig"]["responseMimeType"] == "application/json"
    assert "tools" not in json_body


def test_grounding_sources_and_footer() -> None:
    metadata = {
        "groundingChunks": [
            {"web": {"uri": "https://example.com/a", "title": "Example A"}},
            {"web": {"uri": "https://example.com/a", "title": "Duplicate"}},
            {"web": {"uri": "https://example.com/b", "title": "Example B"}},
        ]
    }
    sources = grounding_sources(metadata)
    assert len(sources) == 2
    footer = citation_footer(sources)
    assert "参考来源" in footer
    assert "https://example.com/a" in footer


@pytest.mark.asyncio
async def test_native_stream_text_and_citations() -> None:
    captured: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _response([
            {"candidates": [{"content": {"parts": [{"text": "答案"}]}}]},
            {"candidates": [{
                "content": {"parts": [{"text": "。"}]},
                "groundingMetadata": {
                    "groundingChunks": [{"web": {"uri": "https://example.com/x", "title": "X"}}]
                },
                "finishReason": "STOP",
            }], "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 2,
                "thoughtsTokenCount": 3,
                "cachedContentTokenCount": 4,
                "totalTokenCount": 15,
            }},
        ])

    cfg = _cfg()
    router = LLMRouter(cfg, transport=httpx.MockTransport(handler))
    out = "".join([tok async for tok in router.stream_chat(
        [{"role": "user", "content": "hi"}], profile_name="g"
    )])
    assert "答案" in out
    assert "参考来源" in out
    assert captured["contents"][0]["parts"][0]["text"] == "hi"
    assert router.last_usage is not None
    assert router.last_usage.reasoning_tokens == 3
    assert router.last_usage.prompt_cache_hit_tokens == 4


@pytest.mark.asyncio
async def test_native_tool_call_captures_signature() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response([
            {"candidates": [{"content": {"parts": [{
                "functionCall": {"name": "get_time", "args": {}, "id": "c1"},
                "thoughtSignature": "sig-1",
            }]}, "finishReason": "STOP"}]},
        ])

    cfg = _cfg()
    router = LLMRouter(cfg, transport=httpx.MockTransport(handler))
    res = await router.complete_with_tools(
        [{"role": "user", "content": "time"}],
        [{"type": "function", "function": {"name": "get_time", "parameters": {"type": "object"}}}],
        profile_name="g",
    )
    assert len(res.tool_calls) == 1
    assert res.tool_calls[0].name == "get_time"
    assert res.tool_calls[0].id == "c1"
    assert res.tool_calls[0].thought_signature == "sig-1"


def test_gemini4_cost_estimate() -> None:
    assert estimate_cny_cost("gemini-4-hydrogen", 1_000_000, 0, 0) == 10.8
    assert estimate_cny_cost("gemini-4-neon", 1_000_000, 0, 0) == 21.6
    assert estimate_cny_cost("gemini-4-carbon", 1_000_000, 0, 1_000_000) == 172.8


class _TimeTool(Tool):
    name = "get_time"
    description = "Return the current time"
    default_risk: RiskLevel = "safe"
    parameters_schema = {"type": "object", "properties": {}}

    async def execute(self, arguments, ctx):
        return ToolResult(ok=True, output="12:00", summary="时间 12:00")


@pytest.mark.asyncio
async def test_native_tool_loop_preserves_signature_and_response() -> None:
    requests: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        has_response = any(
            any("functionResponse" in part for part in content.get("parts", []))
            for content in body.get("contents", [])
        )
        if not has_response:
            return _response([
                {"candidates": [{"content": {"parts": [{
                    "functionCall": {"name": "get_time", "args": {}, "id": "c1"},
                    "thoughtSignature": "sig-1",
                }]}, "finishReason": "STOP"}]},
            ])
        return _response([
            {"candidates": [{"content": {"parts": [{"text": "现在是 12:00。"}]}, "finishReason": "STOP"}]},
        ])

    cfg = _cfg()
    router = LLMRouter(cfg, transport=httpx.MockTransport(handler))
    executor = ToolExecutor(router)
    ctx = ToolExecutionContext(turn_id="t", session_id="s", config=cfg)
    result = await executor.run(
        messages=[{"role": "user", "content": "几点？"}],
        registry=ToolRegistry([_TimeTool()]),
        ctx=ctx,
        profile_name="g",
    )

    assert result.final_text == "现在是 12:00。"
    assert result.called_tools == ["get_time"]
    assert len(requests) == 2
    second = requests[1]
    contents = second["contents"]
    assert contents[1]["parts"][0]["functionCall"]["name"] == "get_time"
    assert contents[1]["parts"][0]["thoughtSignature"] == "sig-1"
    assert contents[2]["parts"][0]["functionResponse"]["name"] == "get_time"
    assert contents[2]["parts"][0]["functionResponse"]["response"] == {"result": "12:00"}
