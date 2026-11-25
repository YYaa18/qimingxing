from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from brain.config import AppConfig, ProfileConfig, is_local_provider
from brain.gemini_native import GEMINI_API_ROOT, build_request as build_gemini_request, is_gemini_native_profile


def is_deepseek_profile(profile: ProfileConfig) -> bool:
    return (
        (profile.provider_id or "").strip().lower() == "deepseek"
        or "api.deepseek.com" in (profile.base_url or "").lower()
    )


def _with_latest_reminder(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not messages:
        return messages
    last_user = -1
    for idx, msg in enumerate(messages):
        if msg.get("role") == "user":
            last_user = idx
    if last_user < 0:
        return messages
    if last_user > 0 and messages[last_user - 1].get("role") == "latest_reminder":
        return messages
    now = _dt.datetime.now().astimezone()
    weekday = "一二三四五六日"[now.weekday()]
    reminder = f"{now:%Y-%m-%d},星期{weekday},本地,App,中文"
    prepared = [dict(m) for m in messages]
    prepared.insert(last_user, {"role": "latest_reminder", "content": reminder})
    return prepared


def deepseek_body_extras(
    config: AppConfig,
    profile: ProfileConfig,
    *,
    json_mode: bool = False,
) -> Dict[str, Any]:
    ds = config.deepseek
    extras: Dict[str, Any] = {}
    if json_mode:
        extras["thinking"] = {"type": "disabled"}
        extras["response_format"] = {"type": "json_object"}
        if ds.user_id:
            extras["user_id"] = ds.user_id
        return extras
    thinking = (ds.thinking or "auto").strip().lower()
    slot_effort = (getattr(profile, "reasoning_effort", "") or "").strip().lower()
    effort = slot_effort or (ds.reasoning_effort or "").strip().lower()
    if slot_effort == "none":
        extras["thinking"] = {"type": "disabled"}
        if ds.user_id:
            extras["user_id"] = ds.user_id
        return extras
    if thinking == "disabled":
        extras["thinking"] = {"type": "disabled"}
    elif thinking == "enabled" or effort:
        extras["thinking"] = {"type": "enabled"}
    if effort and thinking != "disabled":
        extras["reasoning_effort"] = effort
    if ds.safety_check:
        extras["safety_check"] = {"level": "high"}
    if ds.user_id:
        extras["user_id"] = ds.user_id
    return extras


def deepseek_thinking_active(config: AppConfig) -> bool:
    return (config.deepseek.thinking or "auto").strip().lower() != "disabled"


def normalize_cached_input_tokens(usage: Dict[str, Any], prompt_tokens: int) -> int:
    if not isinstance(usage, dict):
        return 0
    details = usage.get("prompt_tokens_details") or {}
    for raw in (
        usage.get("prompt_cache_hit_tokens"),
        details.get("cached_tokens") if isinstance(details, dict) else None,
        usage.get("cache_read_input_tokens"),
    ):
        try:
            value = int(raw or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return max(0, min(int(prompt_tokens or 0), value))
    return 0


@dataclass
class ProviderRequest:
    url: str
    headers: Dict[str, str]
    body: Dict[str, Any]
    style: str = "openai"


class ProviderAdapter:
    """Protocol-specific request builder and usage normalizer.

    Streaming/tool parsing stays in LLMRouter for now, but new providers can
    be added by subclassing this and registering it in resolve_provider_adapter.
    """

    style = "openai"

    def build_request(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        profile: ProfileConfig,
        config: AppConfig,
        *,
        json_mode: bool = False,
    ) -> ProviderRequest:
        raise NotImplementedError

    def cached_input_tokens(self, usage: Dict[str, Any], prompt_tokens: int) -> int:
        return normalize_cached_input_tokens(usage, prompt_tokens)

    def timeout_seconds(self, profile: ProfileConfig) -> float:
        return float(profile.timeout_seconds or 30.0)


class OpenAICompatibleAdapter(ProviderAdapter):
    style = "openai"

    def _api_key(self, profile: ProfileConfig) -> str:
        key = profile.api_key
        if not key and is_local_provider(profile.provider_id, profile.base_url, profile.provider):
            key = "ollama"
        return key

    def build_request(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        profile: ProfileConfig,
        config: AppConfig,
        *,
        json_mode: bool = False,
    ) -> ProviderRequest:
        key = self._api_key(profile)
        if not key:
            raise ValueError(f"模型配置 {profile.name} ({profile.model}) 未设置 API Key。")
        if not profile.base_url:
            raise ValueError(f"模型配置 {profile.name} 缺少 base_url。")
        body: Dict[str, Any] = {
            "model": profile.model,
            "messages": messages,
            "temperature": profile.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return ProviderRequest(
            url=f"{profile.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            body=body,
            style="openai",
        )


class DeepSeekAdapter(OpenAICompatibleAdapter):
    def build_request(self, messages, tools, profile, config, *, json_mode=False):
        request = super().build_request(
            messages, tools, profile, config, json_mode=json_mode
        )
        request_messages = (
            messages if json_mode else _with_latest_reminder(messages)
        )
        body = dict(request.body)
        body["messages"] = request_messages
        body.update(deepseek_body_extras(config, profile, json_mode=json_mode))
        if not deepseek_thinking_active(config):
            body["temperature"] = profile.temperature
        else:
            body.pop("temperature", None)
        return ProviderRequest(
            url=request.url, headers=request.headers, body=body, style="openai"
        )

    def timeout_seconds(self, profile: ProfileConfig) -> float:
        return max(float(profile.timeout_seconds or 0), 120.0)


class GeminiNativeAdapter(ProviderAdapter):
    style = "gemini"

    def build_request(self, messages, tools, profile, config, *, json_mode=False):
        if not profile.api_key:
            raise ValueError(f"模型配置 {profile.name} ({profile.model}) 未设置 API Key。")
        body = build_gemini_request(
            messages, tools, profile, config, json_mode=json_mode
        )
        return ProviderRequest(
            url=(
                f"{GEMINI_API_ROOT}/models/{profile.model}:streamGenerateContent"
                "?alt=sse"
            ),
            headers={
                "x-goog-api-key": profile.api_key,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            body=body,
            style="gemini",
        )

    def cached_input_tokens(self, usage: Dict[str, Any], prompt_tokens: int) -> int:
        raw = (usage or {}).get("cachedContentTokenCount", 0) if isinstance(usage, dict) else 0
        try:
            value = int(raw or 0)
        except (TypeError, ValueError):
            value = 0
        return max(0, min(int(prompt_tokens or 0), value))

    def timeout_seconds(self, profile: ProfileConfig) -> float:
        return max(float(profile.timeout_seconds or 0), 120.0)


class AnthropicAdapter(ProviderAdapter):
    style = "anthropic"

    def build_request(self, messages, tools, profile, config, *, json_mode=False):
        if not profile.api_key:
            raise ValueError(f"模型配置 {profile.name} ({profile.model}) 未设置 API Key。")
        base_url = profile.base_url or "https://api.anthropic.com"
        system_parts: List[str] = []
        chat_messages: List[Dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") == "system":
                system_parts.append(str(msg.get("content", "")))
                continue
            raw_content = msg.get("content", "")
            if isinstance(raw_content, list):
                blocks: List[Dict[str, Any]] = []
                for part in raw_content:
                    if part.get("type") == "text":
                        blocks.append({"type": "text", "text": str(part.get("text", ""))})
                    elif part.get("type") == "image_url":
                        url_val = str((part.get("image_url") or {}).get("url", ""))
                        if url_val.startswith("data:") and ";base64," in url_val:
                            header_part, b64_data = url_val.split(";base64,", 1)
                            media_type = header_part[5:] or "image/jpeg"
                            blocks.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": b64_data,
                                },
                            })
                chat_messages.append({"role": msg.get("role", "user"), "content": blocks})
            else:
                chat_messages.append({"role": msg.get("role", "user"), "content": str(raw_content)})
        body = {
            "model": profile.model,
            "max_tokens": 2048,
            "system": "\n\n".join(system_parts),
            "messages": chat_messages,
            "temperature": profile.temperature,
            "stream": True,
        }
        return ProviderRequest(
            url=f"{base_url}/v1/messages",
            headers={
                "x-api-key": profile.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            body=body,
            style="anthropic",
        )


def resolve_provider_adapter(profile: ProfileConfig) -> ProviderAdapter:
    if is_gemini_native_profile(profile):
        return GeminiNativeAdapter()
    if (profile.provider or "").strip().lower() == "anthropic":
        return AnthropicAdapter()
    if is_deepseek_profile(profile):
        return DeepSeekAdapter()
    return OpenAICompatibleAdapter()
