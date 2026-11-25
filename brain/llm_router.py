from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

import httpx

from brain.config import AppConfig, ProfileConfig, is_local_provider
from protocol.models import ProfileInfo

logger = logging.getLogger("qimingxing.llm_router")


def estimate_cny_cost(
    model: str,
    prompt_tokens: int,
    cache_hit_tokens: int,
    completion_tokens: int,
    profile: Optional[ProfileConfig] = None,
) -> float:
    """Estimate RMB (CNY) API cost for soft usage alerts (does not enforce hard caps)."""
    miss_tokens = max(0, prompt_tokens - max(0, cache_hit_tokens))
    hit_tokens = max(0, min(prompt_tokens, cache_hit_tokens))
    out_tokens = max(0, completion_tokens)
    lower = (model or "").lower()
    if "gpt-4o" in lower or "claude" in lower:
        default_in, default_hit, default_out = 18.0, 9.0, 72.0
    else:
        default_in, default_hit, default_out = 2.0, 0.2, 8.0

    in_rate = (
        profile.input_cny_per_1m
        if profile is not None and profile.input_cny_per_1m is not None
        else default_in
    )
    hit_rate = (
        profile.cache_hit_cny_per_1m
        if profile is not None and profile.cache_hit_cny_per_1m is not None
        else default_hit
    )
    out_rate = (
        profile.output_cny_per_1m
        if profile is not None and profile.output_cny_per_1m is not None
        else default_out
    )
    return (miss_tokens * in_rate + hit_tokens * hit_rate + out_tokens * out_rate) / 1_000_000.0


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    prompt_cache_hit_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_cny: float = 0.0
    model: str = ""
    profile: str = ""
    provider: str = ""
    slot: str = ""


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments_json: str

    def parsed_arguments(self) -> Dict[str, Any]:
        if not self.arguments_json or not self.arguments_json.strip():
            return {}
        try:
            obj = json.loads(self.arguments_json)
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            return {}


@dataclass
class LLMCompletionResult:
    content: str = ""
    tool_calls: List[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: LLMUsage = field(default_factory=LLMUsage)


def _parse_usage(
    raw_usage: Dict[str, Any],
    model: str,
    profile_name: str,
    profile: Optional[ProfileConfig] = None,
) -> LLMUsage:
    prompt_tokens = int(raw_usage.get("prompt_tokens", 0) or 0)
    details = raw_usage.get("prompt_tokens_details") or {}
    cache_hit = int(
        raw_usage.get("prompt_cache_hit_tokens", 0)
        or details.get("cached_tokens", 0)
        or 0
    )
    completion_tokens = int(raw_usage.get("completion_tokens", 0) or 0)
    total_tokens = int(
        raw_usage.get("total_tokens", prompt_tokens + completion_tokens) or 0
    )
    return LLMUsage(
        prompt_tokens=prompt_tokens,
        prompt_cache_hit_tokens=cache_hit,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        estimated_cost_cny=estimate_cny_cost(
            model, prompt_tokens, cache_hit, completion_tokens, profile=profile
        ),
        model=model,
        profile=profile_name,
        provider=(profile.provider_id or profile.provider) if profile is not None else "",
        slot=(profile.slot or "chat") if profile is not None else "",
    )


class LLMError(Exception):
    """Sanitized upstream LLM error safe for forwarding to Body via system.error."""

    def __init__(self, code: str, message: str, recoverable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.recoverable = recoverable


class LLMRouter:
    """Multi-profile cloud LLM streaming router with hot-switch, tool calling, and cancellation."""

    def __init__(
        self,
        config: AppConfig,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._config = config
        self._active_profile: str = config.default_profile
        self.last_usage: Optional[LLMUsage] = None
        self._transport = transport

    @property
    def active_profile(self) -> str:
        return self._active_profile

    def get_profile(self, name: Optional[str] = None) -> ProfileConfig:
        if name and name.startswith("@slot:"):
            return self._config.resolve_slot_profile(name[6:])
        if name is None and self._config.has_explicit_routing and self._active_profile in ("routed_chat", ""):
            return self._config.resolve_slot_profile("chat")
        target = name or self._active_profile
        if target not in self._config.profiles:
            if target == "routed_chat" or self._config.has_explicit_routing:
                return self._config.resolve_slot_profile("chat")
            raise LLMError("PROFILE_NOT_FOUND", f"未找到模型配置: {target}")
        return self._config.profiles[target]

    def set_active_profile(self, name: str) -> ProfileConfig:
        if name not in self._config.profiles:
            raise LLMError("PROFILE_NOT_FOUND", f"未找到模型配置: {name}")
        self._active_profile = name
        profile = self._config.profiles[name]
        logger.info(
            "Switched active LLM profile -> profile=%s provider=%s model=%s",
            profile.name,
            profile.provider,
            profile.model,
        )
        return profile

    def reload_config(self, config: AppConfig) -> None:
        """Hot-replace router config & profiles while keeping a valid active profile."""
        self._config = config
        if config.default_profile and config.default_profile in config.profiles:
            self._active_profile = config.default_profile
        elif self._active_profile not in config.profiles and config.profiles:
            self._active_profile = next(iter(config.profiles))

    def list_public_profiles(self) -> List[ProfileInfo]:
        return self._config.list_public_profiles()

    async def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        profile_name: Optional[str] = None,
    ) -> AsyncIterator[str]:
        profile = self.get_profile(profile_name)
        started = time.perf_counter()
        logger.info(
            "Starting LLM stream: profile=%s provider=%s model=%s",
            profile.name,
            profile.provider,
            profile.model,
        )

        try:
            if profile.provider == "mock":
                gen = self._stream_mock(messages, profile)
            elif profile.provider == "openai_compatible":
                gen = self._stream_openai_compatible(messages, profile)
            elif profile.provider == "anthropic":
                gen = self._stream_anthropic(messages, profile)
            else:
                raise LLMError("UNSUPPORTED_PROVIDER", f"不支持的 provider: {profile.provider}")

            try:
                async for token in gen:
                    yield token
            finally:
                if hasattr(gen, "aclose"):
                    await gen.aclose()

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "Completed LLM stream: profile=%s provider=%s model=%s elapsed_ms=%d",
                profile.name,
                profile.provider,
                profile.model,
                elapsed_ms,
            )
        except asyncio.CancelledError:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "Cancelled upstream LLM stream: profile=%s provider=%s model=%s elapsed_ms=%d",
                profile.name,
                profile.provider,
                profile.model,
                elapsed_ms,
            )
            raise
        except LLMError:
            raise
        except httpx.TimeoutException as exc:
            safe_msg = self._config.redact_secrets(f"上游模型请求超时 ({profile.model}): {exc}")
            raise LLMError("LLM_TIMEOUT", safe_msg) from exc
        except httpx.HTTPError as exc:
            safe_msg = self._config.redact_secrets(f"上游模型网络错误 ({profile.model}): {exc}")
            raise LLMError("LLM_NETWORK_ERROR", safe_msg) from exc
        except Exception as exc:
            safe_msg = self._config.redact_secrets(f"上游模型调用异常 ({profile.model}): {exc}")
            raise LLMError("LLM_INTERNAL_ERROR", safe_msg) from exc

    async def _stream_openai_compatible(
        self,
        messages: List[Dict[str, Any]],
        profile: ProfileConfig,
    ) -> AsyncIterator[str]:
        api_key = profile.api_key
        if not api_key and is_local_provider(profile.provider_id, profile.base_url, profile.provider):
            api_key = "ollama"
        if not api_key:
            raise LLMError(
                "MISSING_API_KEY",
                f"模型配置 {profile.name} ({profile.model}) 未设置 API Key 环境变量。",
            )
        if not profile.base_url:
            raise LLMError("INVALID_CONFIG", f"模型配置 {profile.name} 缺少 base_url。")

        url = f"{profile.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": profile.model,
            "messages": messages,
            "temperature": profile.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        self.last_usage = None

        timeout = httpx.Timeout(profile.timeout_seconds, connect=10.0)
        client_kwargs: Dict[str, Any] = {"timeout": timeout}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        logged_reasoning = False
        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    raw_err = (await resp.aread()).decode("utf-8", errors="replace")
                    safe_err = self._config.redact_secrets(raw_err[:300])
                    raise LLMError(
                        f"UPSTREAM_HTTP_{resp.status_code}",
                        f"上游 API 返回 HTTP {resp.status_code}: {safe_err}",
                    )
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(chunk.get("usage"), dict):
                        self.last_usage = _parse_usage(
                            chunk["usage"], profile.model, profile.name, profile=profile
                        )
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    if delta.get("reasoning_content") and not logged_reasoning:
                        logged_reasoning = True
                        logger.debug(
                            "Ignoring reasoning_content stream from profile=%s model=%s",
                            profile.name,
                            profile.model,
                        )
                    content = delta.get("content")
                    if content:
                        yield str(content)

    async def _stream_anthropic(
        self,
        messages: List[Dict[str, Any]],
        profile: ProfileConfig,
    ) -> AsyncIterator[str]:
        if not profile.api_key:
            raise LLMError(
                "MISSING_API_KEY",
                f"模型配置 {profile.name} ({profile.model}) 未设置 API Key 环境变量。",
            )
        base_url = profile.base_url or "https://api.anthropic.com"
        url = f"{base_url}/v1/messages"

        system_parts: List[str] = []
        chat_messages: List[Dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") == "system":
                system_parts.append(str(msg.get("content", "")))
            else:
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
                                blocks.append(
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": media_type,
                                            "data": b64_data,
                                        },
                                    }
                                )
                    chat_messages.append({"role": msg.get("role", "user"), "content": blocks})
                else:
                    chat_messages.append(
                        {"role": msg.get("role", "user"), "content": str(raw_content)}
                    )

        headers = {
            "x-api-key": profile.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        body = {
            "model": profile.model,
            "max_tokens": 2048,
            "system": "\n\n".join(system_parts),
            "messages": chat_messages,
            "temperature": profile.temperature,
            "stream": True,
        }

        timeout = httpx.Timeout(profile.timeout_seconds, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    raw_err = (await resp.aread()).decode("utf-8", errors="replace")
                    safe_err = self._config.redact_secrets(raw_err[:300])
                    raise LLMError(
                        f"UPSTREAM_HTTP_{resp.status_code}",
                        f"Anthropic API 返回 HTTP {resp.status_code}: {safe_err}",
                    )
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("type") == "content_block_delta":
                        delta = chunk.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield str(delta["text"])
                    elif chunk.get("type") == "message_stop":
                        break

    async def _stream_mock(
        self,
        messages: List[Dict[str, Any]],
        profile: ProfileConfig,
    ) -> AsyncIterator[str]:
        """Local deterministic mock stream for offline testing and demos."""
        sys_msg = str(next((m["content"] for m in messages if m.get("role") == "system"), ""))
        raw_user_msg = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        has_image = False
        if isinstance(raw_user_msg, list):
            text_parts = []
            for item in raw_user_msg:
                if item.get("type") == "text":
                    text_parts.append(str(item.get("text", "")))
                elif item.get("type") == "image_url":
                    has_image = True
            user_msg = "\n".join(text_parts)
        else:
            user_msg = str(raw_user_msg)
        lower_q = user_msg.lower()

        if has_image:
            text = (
                f"[EMOTION:happy]\n"
                f"我看到你发来的桌面截图啦（通过 `{profile.model}` 视觉直看通道），正在帮你看画面里的内容。"
            )
            step = 8
            delay = 0.015
        elif any(k in user_msg for k in ("长文", "500字", "打断", "压测")) or "long" in lower_q:
            text = (
                "[EMOTION:thinking]\n"
                "好的，下面是一段用于测试流式打断与并发控制的较长说明文字。"
                + "启明星正在以流式通道持续输出内容，你可以在任意时刻点击停止或发送新消息来验证 turn_id 取消机制。" * 14
            )
            step = 6
            delay = 0.04
        elif any(k in user_msg for k in ("提示", "攻略", "剧透", "卡片", "表格", "配装")) or any(
            k in lower_q for k in ("hint", "spoiler", "card", "table")
        ):
            text = (
                "[EMOTION:happy]\n"
                "没问题，我把防剧透的分步建议整理到右侧辅助卡片里啦，口语这边只说重点：先稳住血线和站位，观察对手动作前摇再出手。\n"
                '<<<CARD title="Boss 应对提示（防剧透模式）" type="spoiler_steps">>>\n'
                "**第 1 级提示（机制方向）**\n"
                "- 注意观察场地边缘与敌人起手发光信号，不要贪刀。\n"
                "- 中远距离阶段可利用地形掩体规避高伤害投射物。\n\n"
                "| 阶段 | 风险点 | 建议策略 |\n"
                "| --- | --- | --- |\n"
                "| P1 开局 | 远程突进 | 保持中距离横向走位 |\n"
                "| P2 转阶段 | 范围爆发 | 提前后撤并保留翻滚耐力 |\n"
                "<<<END_CARD>>>"
            )
            step = 8
            delay = 0.015
        elif any(k in user_msg for k in ("状态", "角色", "位置", "装备", "窗口", "感官")) or any(
            k in lower_q for k in ("state", "status", "game", "window", "level")
        ):
            combined_ctx = f"{sys_msg}\n{user_msg}"
            marker = "【当前桌面感官上下文】" if "【当前桌面感官上下文】" in combined_ctx else "【当前感官上下文】"
            if marker in combined_ctx:
                tail_section = combined_ctx.split(marker)[-1].split("【本轮用户提问】")[0].strip()
            else:
                tail_section = ""
            if "暂无外部感官上下文" in tail_section or not tail_section:
                text = (
                    "[EMOTION:confused]\n"
                    "我目前还没有收到外部感官模块推送的游戏或窗口状态，你可以先运行 mock_senses/simulator.py 注入模拟状态再问我。"
                )
            else:
                lines = [ln.lstrip("- ").strip() for ln in tail_section.splitlines() if ln.strip()]
                summary = "；".join(lines)
                text = f"[EMOTION:surprised]\n我刚才收到了最新的感官上下文！当前状态记录为：{summary}"
            step = 8
            delay = 0.015
        else:
            text = (
                f"[EMOTION:happy]\n"
                f"你好！我是启明星（当前使用 `{profile.name}` / `{profile.model}`）。"
                f"我已经收到你的消息「{user_msg[:40]}」，三层星型协议链路工作正常！"
            )
            step = 6
            delay = 0.015

        for i in range(0, len(text), step):
            await asyncio.sleep(delay)
            yield text[i : i + step]

    async def complete_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        profile_name: Optional[str] = None,
        on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> LLMCompletionResult:
        """Execute a single chat completion round with native OpenAI tool calling support."""
        profile = self.get_profile(profile_name)
        started = time.perf_counter()
        try:
            if profile.provider == "mock":
                chunks: List[str] = []
                async for tok in self._stream_mock(messages, profile):
                    chunks.append(tok)
                    if on_text_delta is not None:
                        await on_text_delta(tok)
                full = "".join(chunks)
                usage = LLMUsage(
                    model=profile.model,
                    profile=profile.name,
                    provider=profile.provider_id or profile.provider,
                    slot=profile.slot or "chat",
                )
                self.last_usage = usage
                return LLMCompletionResult(content=full, finish_reason="stop", usage=usage)

            if profile.provider == "openai_compatible":
                res = await self._complete_openai_with_tools(
                    messages, tools, profile, on_text_delta=on_text_delta
                )
                self.last_usage = res.usage
                return res

            # Fallback for non-OpenAI providers (e.g. Anthropic): stream text cleanly
            chunks = []
            async for tok in self.stream_chat(messages, profile_name=profile.name):
                chunks.append(tok)
                if on_text_delta is not None:
                    await on_text_delta(tok)
            usage = self.last_usage or LLMUsage(
                model=profile.model,
                profile=profile.name,
                provider=profile.provider_id or profile.provider,
                slot=profile.slot or "chat",
            )
            return LLMCompletionResult(content="".join(chunks), finish_reason="stop", usage=usage)
        except asyncio.CancelledError:
            raise
        except LLMError:
            raise
        except httpx.TimeoutException as exc:
            safe_msg = self._config.redact_secrets(f"上游模型请求超时 ({profile.model}): {exc}")
            raise LLMError("LLM_TIMEOUT", safe_msg) from exc
        except httpx.HTTPError as exc:
            safe_msg = self._config.redact_secrets(f"上游模型网络错误 ({profile.model}): {exc}")
            raise LLMError("LLM_NETWORK_ERROR", safe_msg) from exc
        except Exception as exc:
            safe_msg = self._config.redact_secrets(f"上游模型调用异常 ({profile.model}): {exc}")
            raise LLMError("LLM_INTERNAL_ERROR", safe_msg) from exc
        finally:
            logger.debug(
                "complete_with_tools profile=%s elapsed_ms=%d",
                profile.name,
                int((time.perf_counter() - started) * 1000),
            )

    async def _complete_openai_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        profile: ProfileConfig,
        *,
        on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> LLMCompletionResult:
        api_key = profile.api_key
        if not api_key and is_local_provider(profile.provider_id, profile.base_url, profile.provider):
            api_key = "ollama"
        if not api_key:
            raise LLMError(
                "MISSING_API_KEY",
                f"模型配置 {profile.name} ({profile.model}) 未设置 API Key 环境变量。",
            )
        if not profile.base_url:
            raise LLMError("INVALID_CONFIG", f"模型配置 {profile.name} 缺少 base_url。")

        url = f"{profile.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
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

        timeout = httpx.Timeout(profile.timeout_seconds, connect=10.0)
        client_kwargs: Dict[str, Any] = {"timeout": timeout}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        content_parts: List[str] = []
        tc_acc: Dict[int, Dict[str, Any]] = {}
        finish_reason = "stop"
        usage = LLMUsage(
            model=profile.model,
            profile=profile.name,
            provider=profile.provider_id or profile.provider,
            slot=profile.slot or "chat",
        )

        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    raw_err = (await resp.aread()).decode("utf-8", errors="replace")
                    safe_err = self._config.redact_secrets(raw_err[:300])
                    raise LLMError(
                        f"UPSTREAM_HTTP_{resp.status_code}",
                        f"上游 API 返回 HTTP {resp.status_code}: {safe_err}",
                    )
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(chunk.get("usage"), dict):
                        usage = _parse_usage(
                            chunk["usage"], profile.model, profile.name, profile=profile
                        )
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    if choice.get("finish_reason"):
                        finish_reason = str(choice["finish_reason"])
                    delta = choice.get("delta") or {}
                    text_tok = delta.get("content")
                    if text_tok:
                        tok_str = str(text_tok)
                        content_parts.append(tok_str)
                        if on_text_delta is not None:
                            await on_text_delta(tok_str)
                    for tc_delta in delta.get("tool_calls") or []:
                        idx = int(tc_delta.get("index", 0))
                        entry = tc_acc.setdefault(
                            idx, {"id": "", "name": "", "arguments": []}
                        )
                        if tc_delta.get("id"):
                            entry["id"] = str(tc_delta["id"])
                        fn = tc_delta.get("function") or {}
                        if fn.get("name"):
                            entry["name"] += str(fn["name"])
                        if fn.get("arguments"):
                            entry["arguments"].append(str(fn["arguments"]))

        tool_calls = [
            ToolCallRequest(
                id=entry["id"] or f"call_{idx}",
                name=entry["name"],
                arguments_json="".join(entry["arguments"]),
            )
            for idx, entry in sorted(tc_acc.items(), key=lambda kv: kv[0])
            if entry["name"]
        ]
        return LLMCompletionResult(
            content="".join(content_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )


async def probe_provider_connectivity(
    config: AppConfig,
    *,
    provider_id: str,
    base_url: str = "",
    api_key: str = "",
    protocol: str = "openai_compatible",
    model: str = "",
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Dict[str, Any]:
    """Send a lightweight connectivity check (`GET /models` or minimal completion) to a provider."""
    saved_prov = config.providers.get(provider_id)
    target_url = (base_url or (saved_prov.base_url if saved_prov else "") or "").strip().rstrip("/")
    target_proto = (protocol or (saved_prov.protocol if saved_prov else "openai_compatible")).strip()
    local = is_local_provider(provider_id, target_url, target_proto)

    resolved_key = (api_key or "").strip()
    if (not resolved_key or "****" in resolved_key or "***REDACTED***" in resolved_key) and saved_prov:
        resolved_key = saved_prov.api_key
    if not resolved_key and local:
        resolved_key = "ollama"

    if not target_url or not (target_url.startswith("http://") or target_url.startswith("https://")):
        return {
            "ok": False,
            "provider_id": provider_id,
            "latency_ms": 0,
            "models": [],
            "message": "请先填写有效的以 http:// 或 https:// 开头的 Base URL",
            "error": "INVALID_URL",
        }

    if not resolved_key and not local:
        return {
            "ok": False,
            "provider_id": provider_id,
            "latency_ms": 0,
            "models": [],
            "message": "尚未配置该服务商的 API Key",
            "error": "MISSING_API_KEY",
        }

    headers: Dict[str, str] = {}
    if resolved_key:
        headers["Authorization"] = f"Bearer {resolved_key}"
    if target_proto == "anthropic":
        headers = {"x-api-key": resolved_key, "anthropic-version": "2023-06-01"}

    client_kwargs: Dict[str, Any] = {"timeout": httpx.Timeout(8.0, connect=5.0)}
    if transport is not None:
        client_kwargs["transport"] = transport

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            models_url = f"{target_url}/v1/models" if target_proto == "anthropic" else f"{target_url}/models"
            resp = await client.get(models_url, headers=headers)
            if resp.status_code in (404, 405) and model and target_proto == "openai_compatible":
                resp = await client.post(
                    f"{target_url}/chat/completions",
                    headers={**headers, "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                    },
                )
            latency_ms = max(1, int((time.perf_counter() - started) * 1000))
            if resp.status_code >= 400:
                safe_err = config.redact_secrets(resp.text[:240])
                if resolved_key and len(resolved_key) >= 4:
                    safe_err = safe_err.replace(resolved_key, "***REDACTED***")
                return {
                    "ok": False,
                    "provider_id": provider_id,
                    "latency_ms": latency_ms,
                    "models": [],
                    "message": f"连通失败 (HTTP {resp.status_code}): {safe_err}",
                    "error": f"HTTP_{resp.status_code}",
                }
            models_found: List[str] = []
            try:
                data = resp.json()
                raw_data = data.get("data") if isinstance(data, dict) else None
                if isinstance(raw_data, list):
                    for item in raw_data[:30]:
                        if isinstance(item, dict) and item.get("id"):
                            models_found.append(str(item["id"]))
            except Exception:
                pass
            return {
                "ok": True,
                "provider_id": provider_id,
                "latency_ms": latency_ms,
                "models": models_found,
                "message": f"连通成功 · {latency_ms}ms"
                + (f"（发现 {len(models_found)} 个可用模型）" if models_found else ""),
                "error": "",
            }
    except Exception as exc:
        latency_ms = max(1, int((time.perf_counter() - started) * 1000))
        safe_err = config.redact_secrets(f"{type(exc).__name__}: {exc}")
        if resolved_key and len(resolved_key) >= 4:
            safe_err = safe_err.replace(resolved_key, "***REDACTED***")
        return {
            "ok": False,
            "provider_id": provider_id,
            "latency_ms": latency_ms,
            "models": [],
            "message": f"连接异常: {safe_err}",
            "error": type(exc).__name__,
        }
