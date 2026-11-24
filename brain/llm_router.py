from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from brain.config import AppConfig, ProfileConfig
from protocol.models import ProfileInfo

logger = logging.getLogger("qimingxing.llm_router")


class LLMError(Exception):
    """Sanitized upstream LLM error safe for forwarding to Body via system.error."""

    def __init__(self, code: str, message: str, recoverable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.recoverable = recoverable


class LLMRouter:
    """Multi-profile cloud LLM streaming router with hot-switch and cancellation."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._active_profile: str = config.default_profile

    @property
    def active_profile(self) -> str:
        return self._active_profile

    def get_profile(self, name: Optional[str] = None) -> ProfileConfig:
        target = name or self._active_profile
        if target not in self._config.profiles:
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
        if not profile.api_key:
            raise LLMError(
                "MISSING_API_KEY",
                f"模型配置 {profile.name} ({profile.model}) 未设置 API Key 环境变量。",
            )
        if not profile.base_url:
            raise LLMError("INVALID_CONFIG", f"模型配置 {profile.name} 缺少 base_url。")

        url = f"{profile.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {profile.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": profile.model,
            "messages": messages,
            "temperature": profile.temperature,
            "stream": True,
        }

        timeout = httpx.Timeout(profile.timeout_seconds, connect=10.0)
        logged_reasoning = False
        async with httpx.AsyncClient(timeout=timeout) as client:
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
            marker = "【当前桌面感官上下文】" if "【当前桌面感官上下文】" in sys_msg else "【当前感官上下文】"
            context_section = sys_msg.split(marker)[-1].strip()
            if "暂无外部感官上下文" in context_section or not context_section:
                text = (
                    "[EMOTION:confused]\n"
                    "我目前还没有收到外部感官模块推送的游戏或窗口状态，你可以先运行 mock_senses/simulator.py 注入模拟状态再问我。"
                )
            else:
                lines = [ln.lstrip("- ").strip() for ln in context_section.splitlines() if ln.strip()]
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
