from __future__ import annotations

import base64
import datetime as _dt
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from brain.config import AppConfig, ProfileConfig

GEMINI_API_ROOT = "https://generativelanguage.googleapis.com/v1beta"


def is_gemini_native_profile(profile: ProfileConfig) -> bool:
    """Use native generateContent for the official Gemini endpoint only."""
    base = (profile.base_url or "").lower()
    # Only the official Google host has the native generateContent surface.
    # A custom/proxy Gemini-compatible endpoint must stay on OpenAI-compatible
    # chat completions even if its provider label is "gemini".
    return "generativelanguage.googleapis.com" in base


def _gemini_major(model: str) -> int:
    match = re.search(r"gemini-(\d+)", (model or "").lower())
    return int(match.group(1)) if match else 0


def supports_google_search(model: str) -> bool:
    return _gemini_major(model) >= 3


def supports_thinking_level(model: str) -> bool:
    return _gemini_major(model) >= 3


def _normalized_thinking_level(config: AppConfig, model: str) -> str:
    level = (config.gemini.thinking_level or "auto").strip().lower()
    if level == "auto" or not supports_thinking_level(model):
        return ""
    if level == "xhigh" and not any(tok in (model or "").lower() for tok in ("neon", "hydrogen")):
        level = "high"
    return level.upper()


def _parts_from_content(content: Any) -> List[Dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}] if content else []
    if not isinstance(content, list):
        return []
    parts: List[Dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = str(part.get("type") or "")
        if ptype == "text":
            text = str(part.get("text") or "")
            if text:
                parts.append({"text": text})
        elif ptype == "image_url":
            image = part.get("image_url") or {}
            url = str(image.get("url") or "") if isinstance(image, dict) else ""
            if url.startswith("data:") and ";base64," in url:
                header, data = url.split(";base64,", 1)
                mime = header[5:] or "image/jpeg"
                parts.append({"inlineData": {"mimeType": mime, "data": data}})
            elif url.startswith("http://") or url.startswith("https://"):
                parts.append({"fileData": {"fileUri": url, "mimeType": "image/jpeg"}})
        elif ptype == "input_audio":
            audio = part.get("input_audio") or {}
            if isinstance(audio, dict) and audio.get("data"):
                fmt = str(audio.get("format") or "wav")
                mime = f"audio/{fmt}"
                parts.append({"inlineData": {"mimeType": mime, "data": str(audio["data"])}})
    return parts


def _tool_name_map(messages: List[Dict[str, Any]]) -> Dict[str, str]:
    names: Dict[str, str] = {}
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            call_id = str(call.get("id") or "")
            name = str(fn.get("name") or "")
            if call_id and name:
                names[call_id] = name
    return names


def _function_response_payload(content: Any) -> Dict[str, Any]:
    text = "" if content is None else str(content)
    stripped = text.strip()
    if stripped:
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {"result": text}


def _thought_signature(call: Dict[str, Any]) -> str:
    for key in ("thought_signature", "thoughtSignature"):
        value = call.get(key)
        if value:
            return str(value)
    extra = call.get("extra_content") or {}
    if isinstance(extra, dict):
        google = extra.get("google") or {}
        if isinstance(google, dict):
            value = google.get("thought_signature") or google.get("thoughtSignature")
            if value:
                return str(value)
    return ""


def convert_messages(messages: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    """Convert OpenAI-style messages into Gemini systemInstruction + contents."""
    system_parts: List[str] = []
    contents: List[Dict[str, Any]] = []
    names = _tool_name_map(messages)

    def append(role: str, parts: List[Dict[str, Any]]) -> None:
        if not parts:
            return
        if contents and contents[-1].get("role") == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})

    for msg in messages:
        role = str(msg.get("role") or "")
        if role == "system":
            for part in _parts_from_content(msg.get("content")):
                if part.get("text"):
                    system_parts.append(str(part["text"]))
            continue
        if role in ("latest_reminder",):
            continue
        if role == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            name = names.get(call_id) or "tool"
            response = _function_response_payload(msg.get("content"))
            function_response: Dict[str, Any] = {"name": name, "response": response}
            if call_id:
                function_response["id"] = call_id
            append("user", [{"functionResponse": function_response}])
            continue
        if role == "assistant":
            parts = _parts_from_content(msg.get("content"))
            for call in msg.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                name = str(fn.get("name") or "")
                if not name:
                    continue
                raw_args = fn.get("arguments")
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args) if raw_args.strip() else {}
                    except json.JSONDecodeError:
                        args = {}
                elif isinstance(raw_args, dict):
                    args = raw_args
                else:
                    args = {}
                function_call: Dict[str, Any] = {"name": name, "args": args}
                call_id = str(call.get("id") or "")
                if call_id:
                    function_call["id"] = call_id
                part: Dict[str, Any] = {"functionCall": function_call}
                signature = _thought_signature(call)
                if signature:
                    part["thoughtSignature"] = signature
                parts.append(part)
            append("model", parts)
            continue
        append("user", _parts_from_content(msg.get("content")))

    return "\n\n".join(system_parts).strip(), contents


def convert_tools(
    tools: List[Dict[str, Any]],
    config: AppConfig,
    model: str,
) -> List[Dict[str, Any]]:
    declarations: List[Dict[str, Any]] = []
    supports_server_web = supports_google_search(model)
    native_web = bool(
        supports_server_web and (config.gemini.google_search or config.gemini.url_context)
    )
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        fn = tool.get("function") or {}
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        if native_web and str(fn.get("name")) in ("web_search", "web_fetch"):
            # Native Google Search / URL Context replaces the client-side
            # DeepSeek Search API tools, avoiding duplicate web stacks.
            continue
        declaration: Dict[str, Any] = {
            "name": str(fn["name"]),
            "description": str(fn.get("description") or ""),
        }
        params = fn.get("parameters")
        if isinstance(params, dict):
            declaration["parameters"] = params
        declarations.append(declaration)

    converted: List[Dict[str, Any]] = []
    if declarations:
        converted.append({"functionDeclarations": declarations})
    if config.gemini.google_search and supports_server_web:
        converted.append({"googleSearch": {}})
    if config.gemini.url_context and supports_server_web:
        converted.append({"urlContext": {}})
    return converted


def build_request(
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    profile: ProfileConfig,
    config: AppConfig,
    *,
    json_mode: bool = False,
) -> Dict[str, Any]:
    system_text, contents = convert_messages(messages)
    if system_text and not json_mode:
        now = _dt.datetime.now().astimezone()
        weekday = "一二三四五六日"[now.weekday()]
        system_text = (
            f"{system_text}\n\n【当前日期】{now:%Y-%m-%d} 星期{weekday}（本地时区）"
        )
    body: Dict[str, Any] = {"contents": contents}
    if system_text:
        body["systemInstruction"] = {"parts": [{"text": system_text}]}

    generation: Dict[str, Any] = {}
    if profile.temperature is not None:
        generation["temperature"] = float(profile.temperature)
    if json_mode:
        generation["responseMimeType"] = "application/json"
        # Structured internal tasks stay fast; explicit user levels still win.
        if (config.gemini.thinking_level or "auto").lower() == "auto":
            level = "LOW" if supports_thinking_level(profile.model) else ""
        else:
            level = _normalized_thinking_level(config, profile.model)
    else:
        level = _normalized_thinking_level(config, profile.model)
    if level:
        generation["thinkingConfig"] = {"thinkingLevel": level}
    if generation:
        body["generationConfig"] = generation

    # Internal structured tasks must never invoke web/server tools.
    native_tools = [] if json_mode else convert_tools(tools, config, profile.model)
    if native_tools:
        body["tools"] = native_tools
        has_functions = any("functionDeclarations" in tool for tool in native_tools)
        has_server_tools = any(
            "googleSearch" in tool or "urlContext" in tool for tool in native_tools
        )
        if has_functions and has_server_tools:
            # Gemini requires this when built-in tools and function calling are
            # combined in one request.
            body["toolConfig"] = {"includeServerSideToolInvocations": True}
    return body


def grounding_sources(metadata: Dict[str, Any]) -> List[Dict[str, str]]:
    sources: List[Dict[str, str]] = []
    seen: set[str] = set()
    for chunk in (metadata or {}).get("groundingChunks") or []:
        if not isinstance(chunk, dict):
            continue
        web = chunk.get("web") or {}
        if not isinstance(web, dict):
            continue
        uri = str(web.get("uri") or "").strip()
        if not uri or uri in seen:
            continue
        seen.add(uri)
        sources.append({"url": uri, "title": str(web.get("title") or uri)})
        if len(sources) >= 6:
            break
    return sources


def citation_footer(sources: List[Dict[str, str]]) -> str:
    if not sources:
        return ""
    lines: List[str] = []
    for source in sources:
        title = " ".join(str(source.get("title") or source.get("url") or "").split())
        if len(title) > 90:
            title = title[:87].rstrip() + "…"
        title = title.replace("[", "(").replace("]", ")")
        lines.append(f"- [{title}]({source.get('url')})")
    return "\n\n参考来源：\n" + "\n".join(lines)
