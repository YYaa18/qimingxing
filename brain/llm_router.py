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
from brain.llm.providers import ProviderStreamMixin
from protocol.models import ProfileInfo

logger = logging.getLogger("qimingxing.llm_router")

class LLMRouter(ProviderStreamMixin):
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
