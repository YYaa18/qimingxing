from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from brain.config import AppConfig, ProfileConfig
from brain.providers import normalize_cached_input_tokens

# DeepSeek's chat template can leak its native tool-call markup as plain text
# when the upstream does not convert it into OpenAI tool_calls. Parse it here
# rather than treating the markup as the task's final answer.
_DSML_INVOKE_RE = re.compile(
    r"<[^>]*?DSML[^>]*?\binvoke\b[^>]*?\bname=\"([^\"]+)\"[^>]*>(.*?)</[^>]*?DSML[^>]*?\binvoke\s*>",
    re.DOTALL,
)
_DSML_PARAM_RE = re.compile(
    r"<(?P<attrs>[^>]*?DSML[^>]*?\bparameter\b[^>]*?)>(?P<value>.*?)</[^>]*?DSML[^>]*?\bparameter\s*>",
    re.DOTALL,
)
_DSML_ANY_RE = re.compile(r"<[^>]*?DSML[^>]*?>")


def parse_dsml_tool_calls(content: str) -> tuple[str, List["ToolCallRequest"]]:
    """Extract leaked DSML invokes from text; return cleaned text and calls."""
    text = str(content or "")
    if "DSML" not in text or "invoke" not in text:
        return text, []
    calls: List[ToolCallRequest] = []
    invoke_spans: List[tuple[int, int]] = []
    for match in _DSML_INVOKE_RE.finditer(text):
        name = match.group(1).strip()
        body = match.group(2)
        if not name:
            continue
        args: Dict[str, Any] = {}
        for param in _DSML_PARAM_RE.finditer(body):
            attrs = param.group("attrs")
            name_match = re.search(r"\bname=\"([^\"]+)\"", attrs)
            if not name_match:
                continue
            key = name_match.group(1)
            raw = param.group("value").strip()
            if re.search(r"\bstring=\"(?:true|True|1)\"", attrs):
                args[key] = raw
            else:
                try:
                    args[key] = json.loads(raw)
                except (ValueError, TypeError):
                    args[key] = raw
        calls.append(
            ToolCallRequest(
                id=f"dsml_{len(calls) + 1}",
                name=name,
                arguments_json=json.dumps(args, ensure_ascii=False),
            )
        )
        invoke_spans.append((match.start(), match.end()))
    if not calls:
        return text, []
    cleaned_parts: List[str] = []
    cursor = 0
    for start, end in invoke_spans:
        cleaned_parts.append(text[cursor:start])
        cursor = end
    cleaned_parts.append(text[cursor:])
    cleaned = _DSML_ANY_RE.sub("", "".join(cleaned_parts)).strip()
    return cleaned, calls


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
    if "deepseek-v4-pro" in lower:
        # Peak CNY / 1M tokens. Off-peak is 50%; use peak for a conservative
        # soft-alert estimate.
        default_in, default_hit, default_out = 9.0, 0.30, 27.0
    elif "deepseek" in lower:
        default_in, default_hit, default_out = 2.0, 0.04, 8.0
    elif "gemini-4" in lower:
        # Approximate USD→CNY at 7.2 for soft alerts. Gemini 4 prices are USD:
        # Hydrogen 1.50/0.075/7.50, Neon 3/0.15/15, Carbon/Argon 2/0.10/10.
        if "hydrogen" in lower:
            default_in, default_hit, default_out = 10.8, 0.54, 54.0
        elif "neon" in lower:
            default_in, default_hit, default_out = 21.6, 1.08, 108.0
        else:
            # Carbon/Argon use the post-2026-12-01 conservative rates.
            default_in, default_hit, default_out = 28.8, 1.44, 144.0
    elif "gpt-4o" in lower or "claude" in lower:
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
    cached_input_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    safety_tokens: int = 0
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
    thought_signature: str = ""

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
    reasoning_content: str = ""
    annotations: List[Dict[str, Any]] = field(default_factory=list)
    server_tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    title: str = ""
    refusal: str = ""


def _parse_usage(
    raw_usage: Dict[str, Any],
    model: str,
    profile_name: str,
    profile: Optional[ProfileConfig] = None,
) -> LLMUsage:
    prompt_tokens = int(raw_usage.get("prompt_tokens", 0) or 0)
    cache_hit = normalize_cached_input_tokens(raw_usage, prompt_tokens)
    completion_tokens = int(raw_usage.get("completion_tokens", 0) or 0)
    total_tokens = int(
        raw_usage.get("total_tokens", prompt_tokens + completion_tokens) or 0
    )
    completion_details = raw_usage.get("completion_tokens_details") or {}
    reasoning_tokens = int(completion_details.get("reasoning_tokens", 0) or 0)
    safety_tokens = int(completion_details.get("safety_tokens", 0) or 0)
    return LLMUsage(
        prompt_tokens=prompt_tokens,
        prompt_cache_hit_tokens=cache_hit,
        cached_input_tokens=cache_hit,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        reasoning_tokens=reasoning_tokens,
        safety_tokens=safety_tokens,
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


def _citation_footer(annotations: List[Dict[str, Any]]) -> str:
    sources: List[tuple[str, str]] = []
    seen: set[str] = set()
    for item in annotations or []:
        if not isinstance(item, dict) or item.get("type") != "url_citation":
            continue
        citation = item.get("url_citation") or {}
        if not isinstance(citation, dict):
            continue
        url = str(citation.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        title = " ".join(str(citation.get("title") or url).split())
        if len(title) > 90:
            title = title[:87].rstrip() + "…"
        title = title.replace("[", "(").replace("]", ")")
        sources.append((title, url))
        if len(sources) >= 6:
            break
    if not sources:
        return ""
    lines = [f"- [{title}]({url})" for title, url in sources]
    return "\n\n参考来源：\n" + "\n".join(lines)


def _upstream_error_message(
    config: AppConfig,
    status: int,
    raw: str,
) -> str:
    hints = {
        401: "API Key 无效或已失效。",
        402: "账户余额不足，请充值后重试。",
        408: "请求超时，请稍后重试。",
        413: "请求体或文件过大。",
        422: "请求参数有误。",
        429: "请求速率或并发超限，请稍后重试。",
        500: "服务内部故障，请稍后重试。",
        503: "服务繁忙，请稍后重试。",
        504: "网关超时，请稍后重试。",
    }
    detail = config.redact_secrets((raw or "").strip()[:300])
    hint = hints.get(status, "上游服务返回错误。")
    return f"上游 API 返回 HTTP {status}：{hint}" + (f" {detail}" if detail else "")


