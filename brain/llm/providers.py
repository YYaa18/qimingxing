from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

import httpx

from brain.config import ProfileConfig
from brain.gemini_native import grounding_sources as gemini_grounding_sources
from brain.providers import normalize_cached_input_tokens, resolve_provider_adapter

from brain.llm.models import (
    LLMCompletionResult,
    LLMError,
    LLMUsage,
    ToolCallRequest,
    _upstream_error_message,
    estimate_cny_cost,
)

logger = logging.getLogger("qimingxing.llm.providers")


class ProviderStreamMixin:
    """Gemini/Anthropic/Mock streaming + tool completion (moved verbatim)."""

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

