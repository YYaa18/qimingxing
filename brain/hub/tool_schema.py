from __future__ import annotations

from typing import Any, Dict, List

from brain.config import AppConfig


def build_turn_tools_schema(
    registry: Any,
    config: AppConfig,
    profile: Any,
    *,
    use_tools: bool,
) -> tuple[List[Dict[str, Any]], bool]:
    """Return (tools schema, native_web_tools_enabled) for one turn."""
    schema = registry.to_openai_tools() if use_tools else []
    native_web = bool(
        use_tools
        and config.deepseek.native_web_tools
        and config.tools.web_search.enabled
        and (profile.model or "").lower() == "deepseek-flash"
        and (
            (profile.provider_id or "").lower() == "deepseek"
            or "api.deepseek.com" in (profile.base_url or "").lower()
        )
    )
    if not native_web:
        return schema, False
    schema = [
        tool
        for tool in schema
        if (tool.get("function") or {}).get("name") not in ("web_search", "web_fetch")
    ]
    schema.extend(
        [
            {"type": "deepseek_web_search"},
            {"type": "deepseek_read_url"},
        ]
    )
    return schema, True
