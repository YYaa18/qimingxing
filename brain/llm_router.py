from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

import httpx

from brain.config import AppConfig, ProfileConfig, is_local_provider
from brain.gemini_native import (
    grounding_sources as gemini_grounding_sources,
    is_gemini_native_profile,
)
from brain.providers import (
    normalize_cached_input_tokens,
    resolve_provider_adapter,
)
from brain.llm.models import (
    LLMCompletionResult,
    LLMError,
    LLMUsage,
    ToolCallRequest,
    _citation_footer,
    _parse_usage,
    _upstream_error_message,
    estimate_cny_cost,
    parse_dsml_tool_calls,
)
from protocol.models import ProfileInfo

logger = logging.getLogger("qimingxing.llm_router")

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
        self.last_annotations: List[Dict[str, Any]] = []
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None

    def _http_client(self, timeout: httpx.Timeout) -> httpx.AsyncClient:
        """One keep-alive client per router: avoids a fresh TCP+TLS handshake
        on every turn, tool step and retry."""
        client = self._client
        if client is None or client.is_closed:
            # Requests always pass their own timeout; keep a conservative
            # client default so a future caller without one cannot inherit the
            # short warm-up timeout.
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=10.0),
                transport=self._transport,
                limits=httpx.Limits(
                    max_keepalive_connections=8,
                    keepalive_expiry=300.0,
                ),
            )
            self._client = client
        return client

    @asynccontextmanager
    async def _client_ctx(self, timeout: httpx.Timeout):
        # Shared client: closing the context must not close the pool.
        yield self._http_client(timeout)

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None and not client.is_closed:
            await client.aclose()

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

    async def warm_connection(self) -> None:
        """Open the keep-alive connection before the first user turn.

        The request is allowed to fail (401/404/no network); we only want the
        TCP+TLS handshake out of the first-token critical path.
        """
        try:
            profile = self.get_profile()
        except Exception:
            return
        base_url = (profile.base_url or "").strip()
        if not base_url:
            return
        timeout = httpx.Timeout(5.0, connect=3.0)
        try:
            client = self._http_client(timeout)
            await client.head(base_url, timeout=timeout)
        except Exception:
            return

    async def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        profile_name: Optional[str] = None,
        *,
        json_mode: bool = False,
    ) -> AsyncIterator[str]:
        profile = self.get_profile(profile_name)
        self.last_annotations = []
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
            elif profile.provider == "openai_compatible" and is_gemini_native_profile(profile):
                gen = self._stream_gemini_native(messages, profile, json_mode=json_mode)
            elif profile.provider == "openai_compatible":
                gen = self._stream_openai_compatible(messages, profile, json_mode=json_mode)
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
        *,
        json_mode: bool = False,
    ) -> AsyncIterator[str]:
        adapter = resolve_provider_adapter(profile)
        try:
            request = adapter.build_request(
                messages, [], profile, self._config, json_mode=json_mode
            )
        except ValueError as exc:
            code = "MISSING_API_KEY" if "API Key" in str(exc) else "INVALID_CONFIG"
            raise LLMError(code, str(exc)) from exc
        url = request.url
        headers = request.headers
        body = request.body
        self.last_usage = None

        timeout = httpx.Timeout(adapter.timeout_seconds(profile), connect=10.0)
        logged_reasoning = False
        finish_reason = "stop"
        refusal = ""
        emitted_content = False
        async with self._client_ctx(timeout) as client:
            async with client.stream("POST", url, headers=headers, json=body, timeout=timeout) as resp:
                if resp.status_code >= 400:
                    raw_err = (await resp.aread()).decode("utf-8", errors="replace")
                    raise LLMError(
                        f"UPSTREAM_HTTP_{resp.status_code}",
                        _upstream_error_message(self._config, resp.status_code, raw_err),
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
                    if isinstance(chunk.get("error"), dict):
                        err_msg = str(chunk["error"].get("message") or chunk["error"])
                        raise LLMError(
                            "UPSTREAM_STREAM_ERROR",
                            self._config.redact_secrets(err_msg[:300]),
                        )
                    if isinstance(chunk.get("usage"), dict):
                        self.last_usage = _parse_usage(
                            chunk["usage"], profile.model, profile.name, profile=profile
                        )
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    if choice.get("finish_reason"):
                        finish_reason = str(choice["finish_reason"])
                    delta = choice.get("delta") or {}
                    if delta.get("refusal"):
                        refusal = str(delta["refusal"])
                    if delta.get("reasoning_content") and not logged_reasoning:
                        logged_reasoning = True
                        logger.debug(
                            "Ignoring reasoning_content stream from profile=%s model=%s",
                            profile.name,
                            profile.model,
                        )
                    content = delta.get("content")
                    if content:
                        emitted_content = True
                        yield str(content)
        if not emitted_content:
            if refusal:
                yield f"（内容安全策略拒绝了这次请求：{refusal}）"
            else:
                finish_notes = {
                    "insufficient_system_resource": "（DeepSeek 当前资源紧张，这次没有完成生成，请稍后重试。）",
                    "aborted": "（本次生成被中止，请重试。）",
                    "content_filter": "（内容安全策略拦截了这次回答。）",
                    "length": "（回答达到长度上限，未能完整生成。）",
                }
                note = finish_notes.get(finish_reason, "")
                if note:
                    yield note

    async def _stream_gemini_native(
        self,
        messages: List[Dict[str, Any]],
        profile: ProfileConfig,
        *,
        json_mode: bool = False,
    ) -> AsyncIterator[str]:
        adapter = resolve_provider_adapter(profile)
        try:
            request = adapter.build_request(
                messages, [], profile, self._config, json_mode=json_mode
            )
        except ValueError as exc:
            raise LLMError("MISSING_API_KEY", str(exc)) from exc
        url = request.url
        headers = request.headers
        body = request.body
        timeout = httpx.Timeout(adapter.timeout_seconds(profile), connect=10.0)
        self.last_usage = None
        sources: List[Dict[str, str]] = []
        emitted = False
        finish_reason = "STOP"
        block_reason = ""
        async with self._client_ctx(timeout) as client:
            async with client.stream("POST", url, headers=headers, json=body, timeout=timeout) as resp:
                if resp.status_code >= 400:
                    raw_err = (await resp.aread()).decode("utf-8", errors="replace")
                    raise LLMError(
                        f"UPSTREAM_HTTP_{resp.status_code}",
                        _upstream_error_message(self._config, resp.status_code, raw_err),
                    )
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str or data_str == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(chunk.get("error"), dict):
                        err = chunk["error"]
                        raise LLMError(
                            "UPSTREAM_STREAM_ERROR",
                            self._config.redact_secrets(str(err.get("message") or err)[:300]),
                        )
                    feedback = chunk.get("promptFeedback") or {}
                    if isinstance(feedback, dict) and feedback.get("blockReason"):
                        block_reason = str(feedback["blockReason"])
                    if isinstance(chunk.get("usageMetadata"), dict):
                        self.last_usage = self._parse_gemini_usage(
                            chunk["usageMetadata"], profile
                        )
                    candidates = chunk.get("candidates") or []
                    if not candidates:
                        continue
                    candidate = candidates[0] or {}
                    if candidate.get("finishReason"):
                        finish_reason = str(candidate["finishReason"])
                    grounding = candidate.get("groundingMetadata") or {}
                    if isinstance(grounding, dict):
                        self._merge_gemini_sources(sources, gemini_grounding_sources(grounding))
                    parts = ((candidate.get("content") or {}).get("parts") or [])
                    for part in parts:
                        if not isinstance(part, dict):
                            continue
                        if part.get("thought"):
                            continue
                        text = part.get("text")
                        if text:
                            emitted = True
                            yield str(text)
        if not emitted:
            yield self._gemini_empty_note(finish_reason, block_reason)
        self.last_annotations = [
            self._source_to_url_citation(source)
            for source in sources
        ]

    async def _complete_gemini_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        profile: ProfileConfig,
        *,
        on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> LLMCompletionResult:
        adapter = resolve_provider_adapter(profile)
        try:
            request = adapter.build_request(
                messages, tools, profile, self._config
            )
        except ValueError as exc:
            raise LLMError("MISSING_API_KEY", str(exc)) from exc
        url = request.url
        headers = request.headers
        body = request.body
        timeout = httpx.Timeout(adapter.timeout_seconds(profile), connect=10.0)
        usage = LLMUsage(
            model=profile.model,
            profile=profile.name,
            provider=profile.provider_id or profile.provider,
            slot=profile.slot or "chat",
        )
        content_parts: List[str] = []
        calls: List[ToolCallRequest] = []
        sources: List[Dict[str, str]] = []
        finish_reason = "STOP"
        block_reason = ""
        finish_message = ""
        call_index = 0
        seen_call_ids: set[str] = set()
        async with self._client_ctx(timeout) as client:
            async with client.stream("POST", url, headers=headers, json=body, timeout=timeout) as resp:
                if resp.status_code >= 400:
                    raw_err = (await resp.aread()).decode("utf-8", errors="replace")
                    raise LLMError(
                        f"UPSTREAM_HTTP_{resp.status_code}",
                        _upstream_error_message(self._config, resp.status_code, raw_err),
                    )
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str or data_str == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(chunk.get("error"), dict):
                        err = chunk["error"]
                        raise LLMError(
                            "UPSTREAM_STREAM_ERROR",
                            self._config.redact_secrets(str(err.get("message") or err)[:300]),
                        )
                    feedback = chunk.get("promptFeedback") or {}
                    if isinstance(feedback, dict) and feedback.get("blockReason"):
                        block_reason = str(feedback["blockReason"])
                    if isinstance(chunk.get("usageMetadata"), dict):
                        usage = self._parse_gemini_usage(chunk["usageMetadata"], profile)
                    candidates = chunk.get("candidates") or []
                    if not candidates:
                        continue
                    candidate = candidates[0] or {}
                    if candidate.get("finishReason"):
                        finish_reason = str(candidate["finishReason"])
                    if candidate.get("finishMessage"):
                        finish_message = str(candidate["finishMessage"])
                    grounding = candidate.get("groundingMetadata") or {}
                    if isinstance(grounding, dict):
                        self._merge_gemini_sources(sources, gemini_grounding_sources(grounding))
                    parts = ((candidate.get("content") or {}).get("parts") or [])
                    for part in parts:
                        if not isinstance(part, dict) or part.get("thought"):
                            continue
                        text = part.get("text")
                        if text:
                            content_parts.append(str(text))
                            if on_text_delta is not None:
                                await on_text_delta(str(text))
                        function_call = part.get("functionCall")
                        if isinstance(function_call, dict) and function_call.get("name"):
                            call_id = str(function_call.get("id") or f"call_{call_index}")
                            call_index += 1
                            if call_id in seen_call_ids:
                                continue
                            seen_call_ids.add(call_id)
                            args = function_call.get("args")
                            args_json = json.dumps(args, ensure_ascii=False) if isinstance(args, (dict, list)) else "{}"
                            calls.append(
                                ToolCallRequest(
                                    id=call_id,
                                    name=str(function_call["name"]),
                                    arguments_json=args_json,
                                    thought_signature=str(part.get("thoughtSignature") or ""),
                                )
                            )
        full_content = "".join(content_parts)
        if not full_content.strip():
            note = self._gemini_empty_note(finish_reason, block_reason)
            if note:
                full_content = note
                content_parts.append(note)
                if on_text_delta is not None:
                    await on_text_delta(note)
        if finish_message and finish_message not in full_content and finish_reason in (
            "MALFORMED_FUNCTION_CALL",
            "MISSING_THOUGHT_SIGNATURE",
            "UNEXPECTED_TOOL_CALL",
            "TOO_MANY_TOOL_CALLS",
        ):
            note = f"（Gemini 工具调用失败：{finish_message}）"
            full_content += note
            content_parts.append(note)
            if on_text_delta is not None:
                await on_text_delta(note)
        return LLMCompletionResult(
            content=full_content,
            tool_calls=calls,
            finish_reason=self._gemini_finish_reason(finish_reason),
            usage=usage,
            annotations=[self._source_to_url_citation(s) for s in sources],
            title="",
            refusal="",
        )

    @staticmethod
    def _merge_gemini_sources(
        sources: List[Dict[str, str]],
        incoming: List[Dict[str, str]],
        limit: int = 6,
    ) -> None:
        by_url: Dict[str, Dict[str, str]] = {s["url"]: s for s in sources if s.get("url")}
        for source in incoming:
            url = str(source.get("url") or "").strip()
            if not url:
                continue
            existing = by_url.get(url)
            if existing is not None:
                snippet = str(source.get("snippet") or "").strip()
                if snippet and snippet not in str(existing.get("snippet") or ""):
                    prev = str(existing.get("snippet") or "").strip()
                    existing["snippet"] = (f"{prev} / {snippet}" if prev else snippet)[:160]
                title = str(source.get("title") or "").strip()
                if title and len(title) > len(str(existing.get("title") or "")):
                    existing["title"] = title
                continue
            if len(sources) >= limit:
                continue
            copy = dict(source)
            sources.append(copy)
            by_url[url] = copy

    @staticmethod
    def _source_to_url_citation(source: Dict[str, str]) -> Dict[str, Any]:
        citation: Dict[str, str] = {
            "url": source["url"],
            "title": source["title"],
        }
        snippet = str(source.get("snippet") or "").strip()
        if snippet:
            citation["snippet"] = snippet
        return {"type": "url_citation", "url_citation": citation}

    @staticmethod
    def _gemini_finish_reason(reason: str) -> str:
        mapping = {
            "STOP": "stop",
            "MAX_TOKENS": "length",
            "SAFETY": "content_filter",
            "BLOCKLIST": "content_filter",
            "PROHIBITED_CONTENT": "content_filter",
            "SPII": "content_filter",
            "IMAGE_SAFETY": "content_filter",
            "RECITATION": "content_filter",
        }
        return mapping.get(reason, reason.lower() if reason else "stop")

    @staticmethod
    def _gemini_empty_note(finish_reason: str, block_reason: str) -> str:
        if block_reason:
            return f"（Gemini 拒绝了这次请求：{block_reason}）"
        notes = {
            "SAFETY": "（Gemini 安全策略拦截了这次回答。）",
            "BLOCKLIST": "（Gemini 禁止内容策略拦截了这次回答。）",
            "PROHIBITED_CONTENT": "（Gemini 判定请求包含禁止内容。）",
            "SPII": "（Gemini 判定回答包含敏感个人信息。）",
            "IMAGE_SAFETY": "（Gemini 图片安全策略拦截了这次回答。）",
            "RECITATION": "（Gemini 判定回答涉及受版权保护内容。）",
            "MAX_TOKENS": "（回答达到长度上限，未能完整生成。）",
            "MISSING_THOUGHT_SIGNATURE": "（Gemini 工具调用缺少思考签名，请重试。）",
        }
        return notes.get(finish_reason, "")

    @staticmethod
    def _parse_anthropic_usage(raw: Dict[str, Any], profile: ProfileConfig) -> LLMUsage:
        prompt = max(0, int(raw.get("input_tokens", 0) or 0))
        completion = max(0, int(raw.get("output_tokens", 0) or 0))
        cached = normalize_cached_input_tokens(raw, prompt)
        return LLMUsage(
            prompt_tokens=prompt,
            prompt_cache_hit_tokens=cached,
            cached_input_tokens=cached,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            estimated_cost_cny=estimate_cny_cost(
                profile.model, prompt, cached, completion, profile=profile
            ),
            model=profile.model,
            profile=profile.name,
            provider=profile.provider_id or profile.provider,
            slot=profile.slot or "chat",
        )

    @staticmethod
    def _parse_gemini_usage(raw: Dict[str, Any], profile: ProfileConfig) -> LLMUsage:
        prompt = int(raw.get("promptTokenCount", 0) or 0)
        tool_prompt = int(raw.get("toolUsePromptTokenCount", 0) or 0)
        candidates = int(raw.get("candidatesTokenCount", 0) or 0)
        thoughts = int(raw.get("thoughtsTokenCount", 0) or 0)
        total = int(
            raw.get("totalTokenCount", prompt + tool_prompt + candidates + thoughts) or 0
        )
        prompt_total = prompt + tool_prompt
        try:
            cached_raw = int(raw.get("cachedContentTokenCount", 0) or 0)
        except (TypeError, ValueError):
            cached_raw = 0
        cached = max(0, min(prompt_total, cached_raw))
        completion_total = candidates + thoughts
        return LLMUsage(
            prompt_tokens=prompt_total,
            prompt_cache_hit_tokens=cached,
            cached_input_tokens=cached,
            completion_tokens=completion_total,
            total_tokens=total,
            reasoning_tokens=thoughts,
            estimated_cost_cny=estimate_cny_cost(
                profile.model, prompt_total, cached, completion_total, profile=profile
            ),
            model=profile.model,
            profile=profile.name,
            provider=profile.provider_id or profile.provider,
            slot=profile.slot or "chat",
        )

    async def _stream_anthropic(
        self,
        messages: List[Dict[str, Any]],
        profile: ProfileConfig,
    ) -> AsyncIterator[str]:
        adapter = resolve_provider_adapter(profile)
        try:
            request = adapter.build_request(messages, [], profile, self._config)
        except ValueError as exc:
            raise LLMError("MISSING_API_KEY", str(exc)) from exc
        url = request.url
        headers = request.headers
        body = request.body
        timeout = httpx.Timeout(adapter.timeout_seconds(profile), connect=10.0)
        self.last_usage = None
        async with self._client_ctx(timeout) as client:
            async with client.stream("POST", url, headers=headers, json=body, timeout=timeout) as resp:
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
                    if chunk.get("type") == "message_start":
                        raw_usage = (chunk.get("message") or {}).get("usage") or {}
                        self.last_usage = self._parse_anthropic_usage(raw_usage, profile)
                    elif chunk.get("type") == "message_delta":
                        raw_usage = chunk.get("usage") or {}
                        if isinstance(raw_usage, dict) and raw_usage:
                            merged = dict(raw_usage)
                            if self.last_usage is not None:
                                merged.setdefault("input_tokens", self.last_usage.prompt_tokens)
                            self.last_usage = self._parse_anthropic_usage(merged, profile)
                    elif chunk.get("type") == "content_block_delta":
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
        # The L3 routing/capability sections are part of the user message.
        # Mock intent keywords must only look at the actual question.
        question = user_msg
        if "【本轮用户提问】" in user_msg:
            question = user_msg.rsplit("【本轮用户提问】", 1)[-1].strip()
        lower_q = question.lower()

        if has_image:
            text = (
                f"[EMOTION:happy]\n"
                f"我看到你发来的桌面截图啦（通过 `{profile.model}` 视觉直看通道），正在帮你看画面里的内容。"
            )
            step = 8
            delay = 0.015
        elif any(k in question for k in ("长文", "500字", "打断", "压测")) or "long" in lower_q:
            text = (
                "[EMOTION:thinking]\n"
                "好的，下面是一段用于测试流式打断与并发控制的较长说明文字。"
                + "启明星正在以流式通道持续输出内容，你可以在任意时刻点击停止或发送新消息来验证 turn_id 取消机制。" * 14
            )
            step = 6
            delay = 0.04
        elif any(k in question for k in ("提示", "攻略", "剧透", "卡片", "表格", "配装")) or any(
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
        elif any(k in question for k in ("状态", "角色", "位置", "装备", "窗口", "感官")) or any(
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
                f"我已经收到你的消息「{question[:40]}」，三层星型协议链路工作正常！"
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
        on_server_tool_event: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
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

            if profile.provider == "openai_compatible" and is_gemini_native_profile(profile):
                res = await self._complete_gemini_with_tools(
                    messages,
                    tools,
                    profile,
                    on_text_delta=on_text_delta,
                )
                self.last_usage = res.usage
                return res
            if profile.provider == "openai_compatible":
                res = await self._complete_openai_with_tools(
                    messages,
                    tools,
                    profile,
                    on_text_delta=on_text_delta,
                    on_server_tool_event=on_server_tool_event,
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
        on_server_tool_event: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    ) -> LLMCompletionResult:
        adapter = resolve_provider_adapter(profile)
        try:
            request = adapter.build_request(
                messages, tools, profile, self._config
            )
        except ValueError as exc:
            code = "MISSING_API_KEY" if "API Key" in str(exc) else "INVALID_CONFIG"
            raise LLMError(code, str(exc)) from exc
        url = request.url
        headers = request.headers
        body = request.body
        timeout = httpx.Timeout(adapter.timeout_seconds(profile), connect=10.0)
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        annotations: List[Dict[str, Any]] = []
        server_tool_calls: List[Dict[str, Any]] = []
        response_title = ""
        refusal = ""
        tc_acc: Dict[int, Dict[str, Any]] = {}
        finish_reason = "stop"
        usage = LLMUsage(
            model=profile.model,
            profile=profile.name,
            provider=profile.provider_id or profile.provider,
            slot=profile.slot or "chat",
        )

        async with self._client_ctx(timeout) as client:
            async with client.stream("POST", url, headers=headers, json=body, timeout=timeout) as resp:
                if resp.status_code >= 400:
                    raw_err = (await resp.aread()).decode("utf-8", errors="replace")
                    raise LLMError(
                        f"UPSTREAM_HTTP_{resp.status_code}",
                        _upstream_error_message(self._config, resp.status_code, raw_err),
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
                    if isinstance(chunk.get("error"), dict):
                        err_msg = str(chunk["error"].get("message") or chunk["error"])
                        raise LLMError(
                            "UPSTREAM_STREAM_ERROR",
                            self._config.redact_secrets(err_msg[:300]),
                        )
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
                    event = choice.get("server_tool_event") or chunk.get("server_tool_event")
                    if isinstance(event, dict) and on_server_tool_event is not None:
                        try:
                            await on_server_tool_event(event)
                        except Exception:
                            logger.debug("server tool event callback failed", exc_info=True)
                    delta = choice.get("delta") or {}
                    message = choice.get("message") or {}
                    reasoning_tok = delta.get("reasoning_content") or message.get("reasoning_content")
                    if reasoning_tok:
                        reasoning_parts.append(str(reasoning_tok))
                    merged_annotations = delta.get("annotations") or message.get("annotations")
                    if isinstance(merged_annotations, list):
                        annotations = list(merged_annotations)
                    merged_server_calls = delta.get("server_tool_calls") or message.get("server_tool_calls")
                    if isinstance(merged_server_calls, list):
                        server_tool_calls = list(merged_server_calls)
                    merged_title = delta.get("title") or message.get("title")
                    if merged_title:
                        response_title = str(merged_title)
                    merged_refusal = delta.get("refusal") or message.get("refusal")
                    if merged_refusal:
                        refusal = str(merged_refusal)
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
        full_content = "".join(content_parts)
        if not tool_calls:
            cleaned_content, dsml_calls = parse_dsml_tool_calls(full_content)
            if dsml_calls:
                tool_calls = dsml_calls
                full_content = cleaned_content
        if refusal and not full_content.strip() and not tool_calls:
            refusal_note = f"（内容安全策略拒绝了这次请求：{refusal}）"
            full_content = refusal_note
            content_parts.append(refusal_note)
            if on_text_delta is not None:
                await on_text_delta(refusal_note)
        if not full_content.strip() and not tool_calls:
            finish_notes = {
                "insufficient_system_resource": "（DeepSeek 当前资源紧张，这次没有完成生成，请稍后重试。）",
                "aborted": "（本次生成被中止，请重试。）",
                "content_filter": "（内容安全策略拦截了这次回答。）",
                "length": "（回答达到长度上限，未能完整生成。）",
            }
            fallback = finish_notes.get(finish_reason, "")
            if fallback:
                full_content = fallback
                content_parts.append(fallback)
                if on_text_delta is not None:
                    await on_text_delta(fallback)
        return LLMCompletionResult(
            content=full_content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            reasoning_content="".join(reasoning_parts),
            annotations=annotations,
            server_tool_calls=server_tool_calls,
            title=response_title,
            refusal=refusal,
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
