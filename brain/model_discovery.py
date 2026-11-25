from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from brain.model_catalog import resolve_model_capabilities

logger = logging.getLogger("qimingxing.model_discovery")


def _base_without_v1(base: str) -> str:
    b = (base or "").rstrip("/")
    return b[:-3] if b.endswith("/v1") else b


def _safe_positive_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _caps_item(provider_id: str, model_id: str, label: str, **overrides: Any) -> Dict[str, Any]:
    caps = resolve_model_capabilities(provider_id, model_id)
    return {
        "model_id": model_id,
        "label": label or model_id,
        "context_window": _safe_positive_int(
            overrides.get("context_window"), caps.context_window
        ),
        "max_output_tokens": _safe_positive_int(
            overrides.get("max_output_tokens"), caps.max_output_tokens
        ),
        "supports_tools": bool(overrides.get("supports_tools", caps.supports_tools)),
        "supports_vision": bool(overrides.get("supports_vision", caps.supports_vision)),
        "supports_cache": bool(overrides.get("supports_cache", caps.supports_cache)),
        "supports_reasoning": bool(overrides.get("supports_reasoning", caps.supports_reasoning)),
        "is_preview": bool(overrides.get("is_preview", False)),
        "source": "provider_api",
    }


async def discover_provider_models(
    *,
    provider_id: str,
    protocol: str,
    base_url: str,
    api_key: str,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> List[Dict[str, Any]]:
    """Best-effort live model discovery.

    DeepSeek returns context/output metadata, Gemini ListModels returns
    input/output token limits, while generic OpenAI-compatible endpoints
    usually return only IDs (catalog/manual values are used as fallback).
    """
    pid = (provider_id or "").strip().lower()
    base = (base_url or "").strip().rstrip("/")
    key = (api_key or "").strip()
    headers: Dict[str, str] = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    client_kwargs: Dict[str, Any] = {"timeout": httpx.Timeout(12.0, connect=6.0)}
    if transport is not None:
        client_kwargs["transport"] = transport

    async with httpx.AsyncClient(**client_kwargs) as client:
        if pid == "gemini" or "generativelanguage.googleapis.com" in base:
            url = "https://generativelanguage.googleapis.com/v1beta/models"
            # Gemini accepts API keys via x-goog-api-key. Sending the same key
            # as `Authorization: Bearer` makes Google treat it as an OAuth token
            # and reject the request with 401, so do not forward the generic
            # Bearer header here. Never put the key in the URL query.
            gem_headers = {"x-goog-api-key": key} if key else {}
            resp = await client.get(url, headers=gem_headers, params={"pageSize": 1000})
            resp.raise_for_status()
            data = resp.json() or {}
            results: List[Dict[str, Any]] = []
            for item in data.get("models", []):
                methods = item.get("supportedGenerationMethods") or []
                if "generateContent" not in methods:
                    continue
                name = str(item.get("name") or "").replace("models/", "")
                if not name:
                    continue
                catalog = resolve_model_capabilities(pid, name)
                results.append(_caps_item(
                    pid,
                    name,
                    str(item.get("displayName") or name),
                    context_window=item.get("inputTokenLimit") or catalog.context_window,
                    max_output_tokens=item.get("outputTokenLimit") or catalog.max_output_tokens,
                    supports_tools=True,
                    supports_vision=catalog.supports_vision,
                    supports_cache=catalog.supports_cache,
                    supports_reasoning=catalog.supports_reasoning,
                    is_preview=("preview" in name.lower() or "exp" in name.lower()),
                ))
            return results

        if pid == "deepseek" or "api.deepseek.com" in base:
            urls = [f"{_base_without_v1(base)}/models", f"{base}/models"]
            resp = None
            for url in dict.fromkeys(urls):
                resp = await client.get(url, headers=headers)
                if resp.status_code != 404:
                    break
            assert resp is not None
            resp.raise_for_status()
            data = resp.json() or {}
            results = []
            for item in data.get("data", []):
                mid = str(item.get("id") or "")
                if not mid:
                    continue
                catalog = resolve_model_capabilities(pid, mid)
                modalities = item.get("input_modalities") or []
                results.append(_caps_item(
                    pid,
                    mid,
                    str(item.get("name") or mid),
                    context_window=item.get("context_window") or catalog.context_window,
                    max_output_tokens=item.get("max_output_tokens") or catalog.max_output_tokens,
                    supports_vision=("image" in modalities) or catalog.supports_vision,
                    supports_cache=catalog.supports_cache,
                    supports_reasoning=bool(item.get("effort")) or catalog.supports_reasoning,
                ))
            return results

        if protocol == "anthropic" or "anthropic" in base:
            url = f"{_base_without_v1(base)}/v1/models"
            resp = await client.get(
                url, headers={**headers, "x-api-key": key, "anthropic-version": "2023-06-01"}
            )
            resp.raise_for_status()
            data = resp.json() or {}
            return [
                _caps_item(pid, str(item.get("id") or ""), str(item.get("display_name") or item.get("id") or ""))
                for item in data.get("data", [])
                if item.get("id")
            ]

        if "ollama" in pid or "11434" in base:
            root = _base_without_v1(base)
            resp = await client.get(f"{root}/api/tags", headers=headers)
            resp.raise_for_status()
            data = resp.json() or {}
            names = [
                str(item.get("name") or "")
                for item in data.get("models", [])
                if item.get("name")
            ]
            sem = asyncio.Semaphore(5)

            async def _show_context(name: str) -> int:
                async with sem:
                    try:
                        show = await client.post(
                            f"{root}/api/show",
                            headers=headers,
                            json={"name": name},
                            timeout=2.0,
                        )
                        show.raise_for_status()
                        payload = show.json() or {}
                    except Exception:
                        return 0
                info = payload.get("model_info") or {}
                if isinstance(info, dict):
                    for key, value in info.items():
                        if str(key).endswith("context_length") or str(key) == "context_length":
                            parsed = _safe_positive_int(value, 0)
                            if parsed:
                                return parsed
                params = str(payload.get("parameters") or "")
                match = re.search(r"(?:^|\s)num_ctx\s+(\d+)", params)
                return _safe_positive_int(match.group(1), 0) if match else 0

            contexts = await asyncio.gather(*(_show_context(name) for name in names))
            return [
                _caps_item(pid, name, name, context_window=ctx)
                for name, ctx in zip(names, contexts)
            ]

        # Generic OpenAI-compatible: model IDs only, no context metadata.
        resp = await client.get(f"{base}/models", headers=headers)
        resp.raise_for_status()
        data = resp.json() or {}
        return [
            _caps_item(pid, str(item.get("id") or ""), str(item.get("id") or ""))
            for item in data.get("data", [])
            if item.get("id")
        ]
