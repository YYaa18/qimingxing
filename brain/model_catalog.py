from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ModelCapabilities:
    context_window: int = 0
    max_output_tokens: int = 0
    supports_tools: bool = True
    supports_vision: bool = False
    supports_cache: bool = False
    supports_reasoning: bool = False
    source: str = "default"


# Built-in fallback catalog. Model names and limits change frequently; this is
# only used when a profile has no explicit capability and the provider API is
# not available. Phase B will replace this with live provider discovery and
# per-model overrides in models.db.
_CATALOG: tuple[tuple[str, ModelCapabilities], ...] = (
    # DeepSeek V4 family (current API)
    ("deepseek-v4-pro", ModelCapabilities(1_048_576, 393_216, supports_cache=True, supports_reasoning=True, source="catalog")),
    ("deepseek-v4-flash", ModelCapabilities(1_048_576, 393_216, supports_cache=True, supports_reasoning=True, source="catalog")),
    ("deepseek-flash", ModelCapabilities(1_048_576, 393_216, supports_cache=True, supports_reasoning=True, source="catalog")),
    # Legacy DeepSeek aliases (may be unavailable; keep for migration/fallback)
    ("deepseek-chat", ModelCapabilities(128_000, 8_192, supports_cache=True, source="catalog")),
    ("deepseek-reasoner", ModelCapabilities(128_000, 8_192, supports_cache=True, supports_reasoning=True, source="catalog")),
    # Gemini 3 family
    ("gemini-3", ModelCapabilities(1_048_576, 65_536, supports_vision=True, supports_cache=True, supports_reasoning=True, source="catalog")),
    ("gemini-2.5", ModelCapabilities(1_048_576, 65_536, supports_vision=True, supports_cache=True, supports_reasoning=True, source="catalog")),
    ("gemini-2.0", ModelCapabilities(1_048_576, 8_192, supports_vision=True, supports_cache=True, source="catalog")),
    # Common OpenAI models (fallback only; OpenAI-compatible /models has no limits)
    ("gpt-4o-mini", ModelCapabilities(128_000, 16_384, supports_vision=True, supports_cache=True, source="catalog")),
    ("gpt-4o", ModelCapabilities(128_000, 16_384, supports_vision=True, supports_cache=True, source="catalog")),
    ("o3", ModelCapabilities(200_000, 100_000, supports_reasoning=True, source="catalog")),
    ("o4-mini", ModelCapabilities(200_000, 100_000, supports_reasoning=True, source="catalog")),
    # Anthropic fallback
    ("claude", ModelCapabilities(200_000, 64_000, supports_vision=True, supports_cache=True, source="catalog")),
)


def resolve_model_capabilities(
    provider_id: str,
    model: str,
    *,
    fallback_context_window: int = 64_000,
    fallback_max_output: int = 8_192,
) -> ModelCapabilities:
    """Best-effort capability lookup by provider/model, before Phase B models.db."""
    model_l = (model or "").lower()
    provider_l = (provider_id or "").lower()
    for needle, caps in _CATALOG:
        if needle in model_l:
            return caps
    # Local models: conservative but useful default.
    if "ollama" in provider_l or provider_l in ("local_ollama", "lm_studio"):
        return ModelCapabilities(
            context_window=32_768,
            max_output_tokens=4_096,
            supports_tools=True,
            source="default",
        )
    return ModelCapabilities(
        context_window=max(0, fallback_context_window),
        max_output_tokens=max(0, fallback_max_output),
        source="default",
    )


def catalog_context_window(model: str, provider_id: str = "") -> Optional[int]:
    caps = resolve_model_capabilities(provider_id, model)
    return caps.context_window or None
