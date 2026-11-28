from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlparse

from protocol.models import PrivacyMode, ProfileInfo

from brain.config_defaults import (
    DEFAULT_BLOCKED_APPS,
    DEFAULT_BLOCKED_SHELL_PATTERNS,
    DEFAULT_BLOCKED_TITLE_KEYWORDS,
    DEFAULT_SAFE_SHELL_COMMANDS,
)

def is_local_provider(name: str, base_url: str, protocol: str = "") -> bool:
    """Return True for local keyless endpoints such as mock or Ollama on loopback."""
    proto = (protocol or "").strip().lower()
    if proto == "mock":
        return True
    lname = (name or "").strip().lower()
    if lname in ("mock", "local_ollama", "ollama"):
        return True
    host = (urlparse(base_url or "").hostname or "").strip().lower()
    return host in ("127.0.0.1", "localhost", "::1")


DEFAULT_PROVIDER_SPECS: List[Dict[str, Any]] = [
    {
        "name": "deepseek",
        "label": "DeepSeek",
        "protocol": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "auth_required": True,
        "models": [
            "deepseek-chat",
            "deepseek-reasoner",
            "deepseek-flash",
            "deepseek-ocr",
            "deepseek-asr",
            "deepseek-tts",
        ],
    },
    {
        "name": "siliconflow",
        "label": "硅基流动",
        "protocol": "openai_compatible",
        "base_url": "https://api.siliconflow.cn/v1",
        "auth_required": True,
        "models": [
            "Qwen/Qwen2.5-VL-72B-Instruct",
            "Qwen/Qwen2.5-72B-Instruct",
            "deepseek-ai/DeepSeek-V3",
            "FunAudioLLM/SenseVoiceSmall",
            "FunAudioLLM/CosyVoice2-0.5B",
        ],
    },
    {
        "name": "local_ollama",
        "label": "本地 Ollama",
        "protocol": "openai_compatible",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "ollama",
        "auth_required": False,
        "models": ["qwen2.5:7b", "qwen2.5vl:7b", "llama3.1:8b"],
    },
]


@dataclass
class ProviderConfig:
    name: str
    label: str
    protocol: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    auth_required: bool = True
    models: List[str] = field(default_factory=list)


@dataclass
class SlotRouteConfig:
    provider: str = "deepseek"
    model: str = ""
    voice: str = ""
    # Per-slot extras persisted in models.db: temperature, effort,
    # max_output_override, etc. Kept on the route rather than a profiles table.
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelRoutingConfig:
    chat: SlotRouteConfig = field(
        default_factory=lambda: SlotRouteConfig(provider="deepseek", model="deepseek-chat")
    )
    vision_ocr: SlotRouteConfig = field(
        default_factory=lambda: SlotRouteConfig(provider="deepseek", model="deepseek-flash")
    )
    fast_utility: SlotRouteConfig = field(
        default_factory=lambda: SlotRouteConfig(provider="deepseek", model="deepseek-chat")
    )
    worker: SlotRouteConfig = field(
        default_factory=lambda: SlotRouteConfig(provider="", model="")
    )
    asr: SlotRouteConfig = field(
        default_factory=lambda: SlotRouteConfig(provider="deepseek", model="deepseek-asr")
    )
    tts: SlotRouteConfig = field(
        default_factory=lambda: SlotRouteConfig(
            provider="deepseek", model="deepseek-tts", voice="stella"
        )
    )


@dataclass
class ProfileConfig:
    name: str
    label: str
    provider: str
    model: str
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.7
    timeout_seconds: float = 30.0
    supports_vision: bool = False
    context_window: int = 0
    max_output_tokens: int = 0
    request_max_tokens: int = 0
    reasoning_effort: str = ""
    capability_source: str = "default"
    input_cny_per_1m: Optional[float] = None
    cache_hit_cny_per_1m: Optional[float] = None
    output_cny_per_1m: Optional[float] = None
    provider_id: str = ""
    slot: str = "chat"

    def to_public(self) -> ProfileInfo:
        """Return safe public metadata without secret api_key."""
        return ProfileInfo(
            name=self.name,
            label=self.label,
            provider=self.provider,
            model=self.model,
            supports_vision=self.supports_vision,
        )


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 18790


@dataclass
class OCRConfig:
    provider: str = "deepseek_ocr"
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    mode: str = "text_recognition"
    timeout_seconds: float = 20.0


@dataclass
class PrivacyConfig:
    default_mode: PrivacyMode = "meta_only"
    blocked_apps: List[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_APPS))
    blocked_title_keywords: List[str] = field(
        default_factory=lambda: list(DEFAULT_BLOCKED_TITLE_KEYWORDS)
    )


@dataclass
class MemoryExtractionConfig:
    enabled: bool = True
    profile: str = "fast_deepseek"
    batch_turns: int = 4
    idle_seconds: float = 120.0
    persist_desktop_footprints: bool = True
    # Rolling compression is context infrastructure and runs independently of
    # fact/episode extraction.
    compress_enabled: bool = True
    compress_trigger_turns: int = 10          # legacy fallback only
    compress_batch_turns: int = 6
    compress_high_ratio: float = 1.0           # trigger at recent_budget * ratio
    compress_low_ratio: float = 0.55           # stop after falling below this
    compress_summary_max_chars: int = 1600
    compress_max_batches: int = 4


@dataclass
class EmbeddingConfig:
    provider: str = "null"  # "null" or "openai_compatible"
    base_url: str = "http://127.0.0.1:11434/v1"
    api_key: str = ""
    model: str = "bge-m3"
    timeout_ms: int = 800


@dataclass
class MemoryConfig:
    backend: str = "sqlite"
    sqlite_path: str = "data/qimingxing_memory.db"
    hot_message_limit: int = 10000
    archive_batch_size: int = 2000
    extraction: MemoryExtractionConfig = field(default_factory=MemoryExtractionConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)


@dataclass
class ContextBudgetConfig:
    # --- input budget tiers ---
    input_tier_tokens: int = 32000          # 16000 / 32000 / 64000
    request_max_tokens: int = 8192
    fallback_context_window: int = 64000

    # --- L1 / summary floors + caps ---
    l1_profile_floor_tokens: int = 600
    l1_profile_tokens: int = 1200
    l2_summary_floor_tokens: int = 1000
    l2_summary_tokens: int = 1500
    l2_recent_floor_tokens: int = 3000
    l2_recent_tokens: int = 6000             # legacy fallback

    # --- L3 caps ---
    l3_recall_tokens: int = 800
    l3_skills_tokens: int = 1000
    l3_attachments_tokens: int = 2000
    l3_desktop_ocr_tokens: int = 2000
    l3_total_ratio: float = 0.35            # max share of total_input

    # --- legacy hard cap; used when no resolved budget is supplied ---
    total_max_tokens: int = 32000


@dataclass
class WebSearchToolConfig:
    enabled: bool = True
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    timeout_seconds: float = 15.0
    max_results: int = 5
    max_fetch_chars: int = 6000


@dataclass
class ImageSearchToolConfig:
    enabled: bool = True
    provider: str = "tavily"
    base_url: str = "https://api.tavily.com"
    api_key: str = ""
    timeout_seconds: float = 20.0
    max_results: int = 4
    max_bytes: int = 10 * 1024 * 1024
    safe_search: bool = True
    cache_dir: str = "data/media/image_search"


@dataclass
class ShellToolConfig:
    enabled: bool = True
    safe_commands: List[str] = field(default_factory=lambda: list(DEFAULT_SAFE_SHELL_COMMANDS))
    blocked_patterns: List[str] = field(
        default_factory=lambda: list(DEFAULT_BLOCKED_SHELL_PATTERNS)
    )
    timeout_sec: int = 30
    default_cwd: str = "~"
    max_output_chars: int = 4000


@dataclass
class SkillsConfig:
    auto_learn: bool = True
    max_recalled_skills: int = 2
    learn_profile: str = "fast_deepseek"


@dataclass
class UsageAlertConfig:
    enabled: bool = True
    daily_warn_cny: float = 5.0
    turn_warn_tokens: int = 50000
    hard_block: bool = False  # Soft alert only; never hard-blocks ongoing execution


@dataclass
class ToolsConfig:
    enabled: bool = True
    max_tool_steps: int = 8
    approval_timeout_sec: int = 60
    web_search: WebSearchToolConfig = field(default_factory=WebSearchToolConfig)
    image_search: ImageSearchToolConfig = field(default_factory=ImageSearchToolConfig)
    shell: ShellToolConfig = field(default_factory=ShellToolConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    usage_alert: UsageAlertConfig = field(default_factory=UsageAlertConfig)


@dataclass
class SchedulerConfig:
    enabled: bool = True
    poll_interval_sec: float = 5.0
    summary_profile: str = "fast_deepseek"


@dataclass
class MCPServerConfig:
    name: str
    enabled: bool = True
    transport: Literal["stdio", "sse", "streamable_http"] = "stdio"
    command: str = ""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    safe_tools: List[str] = field(default_factory=list)
    auth_mode: Literal["none", "oauth"] = "none"
    timeout_sec: float = 30.0
    startup_timeout_sec: float = 15.0


@dataclass
class AudioASRConfig:
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-asr"
    timeout_sec: float = 15.0


@dataclass
class AudioTTSConfig:
    enabled: bool = True
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-tts"
    voice: str = "stella"
    sample_rate: int = 24000
    filter_markdown: bool = True
    timeout_sec: float = 20.0


@dataclass
class AudioPTTConfig:
    hotkey: str = "right_option"
    sample_rate: int = 16000
    max_record_sec: int = 60


@dataclass
class AudioConfig:
    enabled: bool = True
    asr: AudioASRConfig = field(default_factory=AudioASRConfig)
    tts: AudioTTSConfig = field(default_factory=AudioTTSConfig)
    ptt: AudioPTTConfig = field(default_factory=AudioPTTConfig)


@dataclass
class BatchContextConfig:
    inbox_dir: str = "~/Desktop/给启明星"
    max_paths: int = 200
    max_tree_depth: int = 3


@dataclass
class GodotConfig:
    auto_hide_on_capture: bool = True
    earcons_enabled: bool = True
    ambient_duck_db: float = -12.0


@dataclass
class UIConfig:
    avatar_enabled: bool = False
    avatar_scale: float = 1.0
    roam: bool = True
    earcons: bool = True
    ambient: bool = False


@dataclass
class BackgroundTaskConfig:
    enabled: bool = True
    max_concurrency: int = 2
    max_steps: int = 24
    timeout_sec: int = 300
    auto_resume_on_restart: bool = True
    # Two-level Semantic Watchdog. Rule screening costs zero tokens; only a
    # rule hit calls the fast utility slot for a semantic verdict.
    watchdog_enabled: bool = True
    watchdog_interval_sec: int = 30
    watchdog_stall_sec: int = 90
    watchdog_max_nudges: int = 2


@dataclass
class BrowserAutomationConfig:
    cdp_endpoint: str = "http://127.0.0.1:9222"
    headless: bool = True
    channel: str = "chrome"


@dataclass
class ShortcutsConfig:
    ptt_hotkey: str = "right_option"
    chat_input_hotkey: str = "option+space"
    cancel_hotkey: str = "option+esc"
    toggle_dock_hotkey: str = "option+h"
    settings_hotkey: str = "option+k"


@dataclass
class DeepSeekConfig:
    """DeepSeek-specific chat features (ignored by other providers)."""

    # auto = don't send thinking (API default: enabled/high)
    # enabled/disabled = explicit override
    thinking: str = "auto"
    reasoning_effort: str = ""  # "" | low | high | max
    native_web_tools: bool = True
    safety_check: bool = False
    # Stable identifier for DeepSeek KVCache isolation / scheduling. Not a
    # personal identifier; override in YAML if multiple users share a key.
    user_id: str = "qimingxing-desktop"


@dataclass
class GeminiConfig:
    """Gemini-native chat features (ignored by other providers)."""

    # auto = don't send thinkingConfig (model default)
    # low/medium/high/xhigh = explicit thinkingLevel
    thinking_level: str = "auto"
    # Google Search grounding and URL context are billed per query/use; off by
    # default, opt-in from Settings.
    google_search: bool = False
    url_context: bool = False


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    default_profile: str = "mock_local"
    profiles: Dict[str, ProfileConfig] = field(default_factory=dict)
    providers: Dict[str, ProviderConfig] = field(default_factory=dict)
    model_routing: ModelRoutingConfig = field(default_factory=ModelRoutingConfig)
    has_explicit_routing: bool = False
    # Phase B: capabilities loaded from models.db, keyed by "provider_id/model_id".
    model_capabilities: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    ocr: OCRConfig = field(default_factory=OCRConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    context_budget: ContextBudgetConfig = field(default_factory=ContextBudgetConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    mcp_servers: List[MCPServerConfig] = field(default_factory=list)
    audio: AudioConfig = field(default_factory=AudioConfig)
    batch_context: BatchContextConfig = field(default_factory=BatchContextConfig)
    godot: GodotConfig = field(default_factory=GodotConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    background_tasks: BackgroundTaskConfig = field(default_factory=BackgroundTaskConfig)
    browser: BrowserAutomationConfig = field(default_factory=BrowserAutomationConfig)
    shortcuts: ShortcutsConfig = field(default_factory=ShortcutsConfig)
    deepseek: DeepSeekConfig = field(default_factory=DeepSeekConfig)
    gemini: GeminiConfig = field(default_factory=GeminiConfig)
    _secrets: List[str] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._ensure_providers_and_routing_defaults()

    def _ensure_providers_and_routing_defaults(self) -> None:
        # Ensure built-in preset providers always exist in the pool
        ds_key = ""
        for p in self.profiles.values():
            if "deepseek" in (p.base_url or "").lower() and p.api_key:
                ds_key = p.api_key
                break
        if not ds_key:
            ds_key = self.ocr.api_key or self.audio.asr.api_key or self.audio.tts.api_key or ""

        for spec in DEFAULT_PROVIDER_SPECS:
            pname = str(spec["name"])
            if pname not in self.providers:
                self.providers[pname] = ProviderConfig(
                    name=pname,
                    label=str(spec["label"]),
                    protocol=str(spec["protocol"]),
                    base_url=str(spec["base_url"]),
                    api_key=ds_key if pname == "deepseek" else str(spec.get("api_key", "")),
                    auth_required=bool(spec.get("auth_required", True)),
                    models=list(spec.get("models", [])),
                )
            elif pname == "deepseek" and not self.providers[pname].api_key and ds_key:
                self.providers[pname].api_key = ds_key

        # Infer custom providers from non-mock profiles when missing
        for p in self.profiles.values():
            if p.provider == "mock" or not p.base_url:
                continue
            inferred_id = p.provider_id or _infer_provider_id_from_url(p.base_url, p.provider)
            if not p.provider_id:
                p.provider_id = inferred_id
            if inferred_id not in self.providers:
                local = is_local_provider(inferred_id, p.base_url, p.provider)
                self.providers[inferred_id] = ProviderConfig(
                    name=inferred_id,
                    label=p.label.split("(")[0].strip() or inferred_id,
                    protocol=p.provider,
                    base_url=p.base_url.rstrip("/"),
                    api_key=p.api_key,
                    auth_required=not local,
                    models=[p.model] if p.model else [],
                )
            else:
                prov = self.providers[inferred_id]
                if not prov.api_key and p.api_key:
                    prov.api_key = p.api_key
                if p.model and p.model not in prov.models:
                    prov.models.append(p.model)

        if not self.has_explicit_routing:
            def_p = self.profiles.get(self.default_profile)
            if def_p and def_p.provider != "mock":
                chat_pid = def_p.provider_id or _infer_provider_id_from_url(def_p.base_url, def_p.provider)
                self.model_routing.chat = SlotRouteConfig(provider=chat_pid, model=def_p.model)
            fast_p = self.profiles.get(self.memory.extraction.profile) or def_p
            if fast_p and fast_p.provider != "mock":
                fast_pid = fast_p.provider_id or _infer_provider_id_from_url(fast_p.base_url, fast_p.provider)
                self.model_routing.fast_utility = SlotRouteConfig(provider=fast_pid, model=fast_p.model)
            if def_p and def_p.provider != "mock" and def_p.supports_vision:
                v_pid = def_p.provider_id or _infer_provider_id_from_url(def_p.base_url, def_p.provider)
                self.model_routing.vision_ocr = SlotRouteConfig(provider=v_pid, model=def_p.model)
            self.model_routing.asr = SlotRouteConfig(
                provider=self.audio.asr.provider or "deepseek",
                model=self.audio.asr.model or "deepseek-asr",
            )
            self.model_routing.tts = SlotRouteConfig(
                provider=self.audio.tts.provider or "deepseek",
                model=self.audio.tts.model or "deepseek-tts",
                voice=self.audio.tts.voice or "stella",
            )

    def _apply_model_caps(self, profile: ProfileConfig) -> ProfileConfig:
        caps = self.model_capabilities.get(
            f"{profile.provider_id}/{profile.model}"
        )
        if not caps:
            return profile
        if not profile.context_window:
            profile.context_window = int(caps.get("context_window") or 0)
        if not profile.max_output_tokens:
            profile.max_output_tokens = int(caps.get("max_output_tokens") or 0)
        profile.capability_source = str(caps.get("source") or "models_db")
        if caps.get("supports_vision") is not None:
            profile.supports_vision = bool(caps.get("supports_vision"))
        return profile

    def resolve_slot_profile(self, slot: str) -> ProfileConfig:
        """Resolve a capability slot (`chat`, `vision_ocr`, `fast_utility`, `asr`, `tts`) to a ProfileConfig."""
        slot_clean = (slot or "chat").strip().lower()
        if slot_clean == "worker":
            route = getattr(self.model_routing, "worker", None)
            if route is None or not route.provider or not route.model:
                return self.resolve_slot_profile("chat")
        if not self.has_explicit_routing:
            if slot_clean == "fast_utility":
                cand = self.profiles.get(self.memory.extraction.profile) or self.profiles.get(self.default_profile)
                if cand is not None:
                    return cand
            elif slot_clean in ("chat", "vision_ocr"):
                cand = self.profiles.get(self.default_profile)
                if cand is not None:
                    return cand

        route = getattr(self.model_routing, slot_clean, self.model_routing.chat)
        prov = self.providers.get(route.provider)
        if prov is None:
            fallback = self.profiles.get(self.default_profile)
            if fallback is not None:
                return fallback
            return self._apply_model_caps(ProfileConfig(
                name=f"slot_{slot_clean}",
                label=route.model or slot_clean,
                provider="openai_compatible",
                provider_id=route.provider,
                model=route.model,
                slot=slot_clean,
            ))

        local = (not prov.auth_required) or is_local_provider(prov.name, prov.base_url, prov.protocol)
        api_key = prov.api_key or ("ollama" if local and prov.protocol == "openai_compatible" else "")
        model_name = route.model or (prov.models[0] if prov.models else "")
        lower_m = model_name.lower()
        is_vision = slot_clean == "vision_ocr" and "ocr" not in lower_m
        if slot_clean == "chat":
            is_vision = any(tok in lower_m for tok in ("vl", "vision", "gpt-4o", "flash", "claude-3"))
        profile = ProfileConfig(
            name="routed_chat" if slot_clean == "chat" else f"slot_{slot_clean}",
            label=f"{prov.label} · {model_name or slot_clean}",
            provider=prov.protocol,
            provider_id=prov.name,
            model=model_name,
            base_url=prov.base_url.rstrip("/"),
            api_key=api_key,
            supports_vision=is_vision,
            slot=slot_clean,
        )
        return self._apply_model_caps(self._apply_slot_extra(profile, route))

    @staticmethod
    def _apply_slot_extra(profile: ProfileConfig, route: SlotRouteConfig) -> ProfileConfig:
        extra = route.extra if isinstance(route.extra, dict) else {}
        temperature = extra.get("temperature")
        if isinstance(temperature, (int, float)):
            profile.temperature = max(0.0, min(2.0, float(temperature)))
        max_override = extra.get("max_output_override")
        if isinstance(max_override, (int, float)) and int(max_override) > 0:
            profile.request_max_tokens = int(max_override)
        effort = str(extra.get("effort") or extra.get("reasoning_effort") or "").strip().lower()
        if effort in ("", "none", "low", "medium", "high", "max", "xhigh"):
            profile.reasoning_effort = effort
        return profile

    def collect_secrets(self) -> List[str]:
        secrets = [p.api_key for p in self.profiles.values() if p.api_key]
        for prov in self.providers.values():
            if prov.api_key and prov.auth_required and prov.api_key != "ollama":
                secrets.append(prov.api_key)
        if self.ocr.api_key:
            secrets.append(self.ocr.api_key)
        if self.memory.embedding.api_key:
            secrets.append(self.memory.embedding.api_key)
        if self.tools.web_search.api_key:
            secrets.append(self.tools.web_search.api_key)
        if self.tools.image_search.api_key:
            secrets.append(self.tools.image_search.api_key)
        if self.audio.asr.api_key:
            secrets.append(self.audio.asr.api_key)
        if self.audio.tts.api_key:
            secrets.append(self.audio.tts.api_key)
        for srv in self.mcp_servers:
            for val in srv.env.values():
                if val:
                    secrets.append(val)
            for val in srv.headers.values():
                if val:
                    secrets.append(val)
        secrets.extend(self._secrets)
        return sorted({s for s in secrets if len(s) >= 4}, key=len, reverse=True)

    def redact_secrets(self, text: str) -> str:
        """Mask any configured API keys or sensitive tokens before returning/logging errors."""
        if not text:
            return text
        sanitized = text
        for secret in self.collect_secrets():
            sanitized = sanitized.replace(secret, "***REDACTED***")
        sanitized = re.sub(r"Bearer\s+[A-Za-z0-9._\-]{8,}", "Bearer ***REDACTED***", sanitized)
        sanitized = re.sub(r"\bsk-[A-Za-z0-9_\-]{8,}\b", "***REDACTED***", sanitized)
        # Credential formats that commonly leak through shell/file output even
        # when they were never configured in this app.
        sanitized = re.sub(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "***REDACTED***", sanitized)
        sanitized = re.sub(
            r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b", "***REDACTED***", sanitized
        )
        sanitized = re.sub(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", "***REDACTED***", sanitized)
        sanitized = re.sub(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b", "***REDACTED***", sanitized)
        sanitized = re.sub(r"\bAIza[0-9A-Za-z_\-]{35}\b", "***REDACTED***", sanitized)
        sanitized = re.sub(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            "***REDACTED***",
            sanitized,
            flags=re.DOTALL,
        )
        return sanitized

    def list_public_profiles(self) -> List[ProfileInfo]:
        return [p.to_public() for p in self.profiles.values()]


def _infer_provider_id_from_url(base_url: str, protocol: str = "") -> str:
    low = (base_url or "").lower()
    if protocol == "mock":
        return "mock"
    if "deepseek" in low:
        return "deepseek"
    if "siliconflow" in low:
        return "siliconflow"
    if "11434" in low or "127.0.0.1" in low or "localhost" in low:
        return "local_ollama"
    if "openai.com" in low:
        return "openai"
    if "anthropic" in low or protocol == "anthropic":
        return "anthropic"
    host = (urlparse(base_url or "").hostname or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9_]+", "_", host).strip("_")
    return cleaned or "custom"


