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
from brain.llm.streaming import OpenAIStreamMixin, probe_provider_connectivity
from protocol.models import ProfileInfo

logger = logging.getLogger("qimingxing.llm_router")

class LLMRouter(ProviderStreamMixin, OpenAIStreamMixin):
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
