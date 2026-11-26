from __future__ import annotations

import httpx

from brain.config import AppConfig, ProfileConfig
from brain.llm_router import LLMRouter
from brain.providers import (
    AnthropicAdapter,
    DeepSeekAdapter,
    GeminiNativeAdapter,
    OpenAICompatibleAdapter,
    normalize_cached_input_tokens,
    resolve_provider_adapter,
)


def _profile(provider: str, provider_id: str, model: str, base: str) -> ProfileConfig:
    return ProfileConfig(
        name="p", label="P", provider=provider, provider_id=provider_id,
        model=model, base_url=base, api_key="sk-test",
    )


def test_adapter_resolution() -> None:
    assert isinstance(resolve_provider_adapter(_profile(
        "openai_compatible", "deepseek", "deepseek-flash", "https://api.deepseek.com/v1"
    )), DeepSeekAdapter)
    assert isinstance(resolve_provider_adapter(_profile(
        "openai_compatible", "gemini", "gemini-4-hydrogen",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
    )), GeminiNativeAdapter)
    assert isinstance(resolve_provider_adapter(_profile(
        "anthropic", "anthropic", "claude", "https://api.anthropic.com"
    )), AnthropicAdapter)
    assert isinstance(resolve_provider_adapter(_profile(
        "openai_compatible", "openai", "gpt-4o", "https://api.openai.com/v1"
    )), OpenAICompatibleAdapter)


def test_deepseek_adapter_request_shape() -> None:
    cfg = AppConfig()
    cfg.deepseek.thinking = "enabled"
    cfg.deepseek.reasoning_effort = "low"
    profile = _profile("openai_compatible", "deepseek", "deepseek-flash", "https://api.deepseek.com/v1")
    request = resolve_provider_adapter(profile).build_request(
        [{"role": "user", "content": "hi"}],
        [{"type": "function", "function": {"name": "x"}}],
        profile, cfg,
    )
    assert request.style == "openai"
    assert request.url.endswith("/chat/completions")
    assert request.body["thinking"] == {"type": "enabled"}
    assert request.body["reasoning_effort"] == "low"
    assert request.body["messages"][-2]["role"] == "latest_reminder"
    assert "temperature" not in request.body
    assert request.body["tools"][0]["function"]["name"] == "x"


def test_deepseek_json_mode_skips_reminder_and_enables_json() -> None:
    cfg = AppConfig()
    cfg.deepseek.thinking = "enabled"
    cfg.deepseek.reasoning_effort = "max"
    profile = _profile("openai_compatible", "deepseek", "deepseek-flash", "https://api.deepseek.com/v1")
    request = resolve_provider_adapter(profile).build_request(
        [{"role": "system", "content": "json"}, {"role": "user", "content": "extract"}],
        [], profile, cfg, json_mode=True,
    )
    assert request.body["thinking"] == {"type": "disabled"}
    assert request.body["response_format"] == {"type": "json_object"}
    assert all(m["role"] != "latest_reminder" for m in request.body["messages"])


def test_gemini_native_adapter_request_shape() -> None:
    cfg = AppConfig()
    cfg.gemini.thinking_level = "high"
    profile = _profile(
        "openai_compatible", "gemini", "gemini-4-hydrogen",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    request = resolve_provider_adapter(profile).build_request(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        [{"type": "function", "function": {"name": "x", "parameters": {"type": "object"}}}],
        profile, cfg,
    )
    assert request.style == "gemini"
    assert "streamGenerateContent" in request.url
    assert request.headers["x-goog-api-key"] == "sk-test"
    assert "Authorization" not in request.headers
    assert request.body["systemInstruction"]["parts"][0]["text"].startswith("sys")
    assert request.body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "HIGH"}
    assert request.body["tools"][0]["functionDeclarations"][0]["name"] == "x"


def test_anthropic_adapter_request_shape_and_cache_normalization() -> None:
    profile = _profile("anthropic", "anthropic", "claude", "https://api.anthropic.com")
    request = resolve_provider_adapter(profile).build_request(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        [], profile, AppConfig(),
    )
    assert request.style == "anthropic"
    assert request.url.endswith("/v1/messages")
    assert request.headers["x-api-key"] == "sk-test"
    assert request.body["system"] == "sys"
    assert normalize_cached_input_tokens({"prompt_tokens": 100, "cache_read_input_tokens": 60}, 100) == 60
    assert normalize_cached_input_tokens({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 40}}, 100) == 40
    assert normalize_cached_input_tokens({"prompt_tokens": 100, "prompt_cache_hit_tokens": 999}, 100) == 100


async def test_router_warm_connection_opens_keepalive_client() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401)

    profile = _profile(
        "openai_compatible", "deepseek", "deepseek-flash", "https://api.deepseek.com/v1"
    )
    cfg = AppConfig(profiles={profile.name: profile}, default_profile=profile.name)
    router = LLMRouter(cfg, transport=httpx.MockTransport(handler))
    await router.warm_connection()
    assert len(seen) == 1
    assert seen[0].method == "HEAD"
    assert str(seen[0].url).startswith("https://api.deepseek.com/v1")
    await router.aclose()


async def test_router_warm_connection_swallows_network_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    profile = _profile(
        "openai_compatible", "deepseek", "deepseek-flash", "https://api.deepseek.com/v1"
    )
    cfg = AppConfig(profiles={profile.name: profile}, default_profile=profile.name)
    router = LLMRouter(cfg, transport=httpx.MockTransport(handler))
    await router.warm_connection()  # must not raise
    await router.aclose()
