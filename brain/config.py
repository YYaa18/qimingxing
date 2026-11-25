from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlparse

import yaml

from protocol.models import PrivacyMode, ProfileInfo

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_BLOCKED_APPS = [
    "1Password",
    "Bitwarden",
    "KeePass",
    "Keychain Access",
    "钥匙串访问",
    "微信",
    "WeChat",
]

DEFAULT_BLOCKED_TITLE_KEYWORDS = [
    "Incognito",
    "InPrivate",
    "Private Browsing",
    "无痕",
    "网银",
    "支付宝",
    "Alipay",
    "Bank",
    "Password",
    "密码",
]


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
    compress_trigger_turns: int = 10
    compress_batch_turns: int = 6


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
    l1_profile_tokens: int = 400
    l2_summary_tokens: int = 350
    l2_recent_tokens: int = 2000
    l3_recall_tokens: int = 500
    l3_desktop_ocr_tokens: int = 1500
    total_max_tokens: int = 6000


DEFAULT_SAFE_SHELL_COMMANDS = [
    "pwd",
    "ls",
    "git status",
    "git log",
    "git diff",
    "which",
    "whoami",
    "uname",
    "date",
    "echo",
    "head",
    "tail",
    "wc",
    "cat",
]

DEFAULT_BLOCKED_SHELL_PATTERNS = [
    r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+(/|\*|~)",
    r"rm\s+-[a-zA-Z]*f[a-zA-Z]*r\s+(/|\*|~)",
    r"\bmkfs(\.[a-z0-9]+)?\b",
    r"\bdd\s+if=",
    r"\bsudo\b",
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r"\bdiskutil\s+(erase|partition|zeroDisk)",
    r"\bprintenv\b",
    r"\benv\b\s*$",
    r"config\.yaml",
    r"\.env\b",
    r":\(\)\s*\{",
    r">\s*/dev/sd",
    r"chmod\s+-R\s+777\s+/",
]


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
class ShortcutsConfig:
    ptt_hotkey: str = "right_option"
    chat_input_hotkey: str = "option+space"
    cancel_hotkey: str = "option+esc"
    toggle_dock_hotkey: str = "option+h"
    settings_hotkey: str = "option+k"


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    default_profile: str = "mock_local"
    profiles: Dict[str, ProfileConfig] = field(default_factory=dict)
    providers: Dict[str, ProviderConfig] = field(default_factory=dict)
    model_routing: ModelRoutingConfig = field(default_factory=ModelRoutingConfig)
    has_explicit_routing: bool = False
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
    shortcuts: ShortcutsConfig = field(default_factory=ShortcutsConfig)
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

    def resolve_slot_profile(self, slot: str) -> ProfileConfig:
        """Resolve a capability slot (`chat`, `vision_ocr`, `fast_utility`, `asr`, `tts`) to a ProfileConfig."""
        slot_clean = (slot or "chat").strip().lower()
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
            return ProfileConfig(
                name=f"slot_{slot_clean}",
                label=route.model or slot_clean,
                provider="openai_compatible",
                provider_id=route.provider,
                model=route.model,
                slot=slot_clean,
            )

        local = (not prov.auth_required) or is_local_provider(prov.name, prov.base_url, prov.protocol)
        api_key = prov.api_key or ("ollama" if local and prov.protocol == "openai_compatible" else "")
        model_name = route.model or (prov.models[0] if prov.models else "")
        lower_m = model_name.lower()
        is_vision = slot_clean == "vision_ocr" and "ocr" not in lower_m
        if slot_clean == "chat":
            is_vision = any(tok in lower_m for tok in ("vl", "vision", "gpt-4o", "flash", "claude-3"))
        return ProfileConfig(
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


def _expand_env_vars(value: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), "")

    return _ENV_VAR_RE.sub(_replace, value)


def resolve_config_path(explicit_path: Optional[Path] = None) -> Path:
    if explicit_path is not None:
        return explicit_path
    env_path = os.environ.get("QIMINGXING_CONFIG")
    if env_path:
        return Path(env_path)
    local_cfg = _REPO_ROOT / "config.yaml"
    if local_cfg.exists():
        return local_cfg
    return _REPO_ROOT / "config.example.yaml"


def load_config(path: Optional[Path] = None) -> AppConfig:
    cfg_path = resolve_config_path(path)
    if not cfg_path.exists():
        return AppConfig(
            profiles={
                "mock_local": ProfileConfig(
                    name="mock_local",
                    label="本地模拟模型 (无需 Key)",
                    provider="mock",
                    model="qimingxing-mock-v1",
                )
            }
        )

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    srv_raw = raw.get("server") or {}
    server = ServerConfig(
        host=str(srv_raw.get("host", "127.0.0.1")),
        port=int(srv_raw.get("port", 18790)),
    )

    profiles: Dict[str, ProfileConfig] = {}
    for name, item in (raw.get("profiles") or {}).items():
        raw_key = str(item.get("api_key", "") or "")
        expanded_key = _expand_env_vars(raw_key)
        raw_in = item.get("input_cny_per_1m")
        raw_hit = item.get("cache_hit_cny_per_1m")
        raw_out = item.get("output_cny_per_1m")
        b_url = str(item.get("base_url", "") or "").rstrip("/")
        proto = str(item.get("provider", "openai_compatible"))
        profiles[name] = ProfileConfig(
            name=name,
            label=str(item.get("label", name)),
            provider=proto,
            model=str(item.get("model", "")),
            base_url=b_url,
            api_key=expanded_key,
            temperature=float(item.get("temperature", 0.7)),
            timeout_seconds=float(item.get("timeout_seconds", 30.0)),
            supports_vision=bool(item.get("supports_vision", False)),
            input_cny_per_1m=float(raw_in) if raw_in is not None else None,
            cache_hit_cny_per_1m=float(raw_hit) if raw_hit is not None else None,
            output_cny_per_1m=float(raw_out) if raw_out is not None else None,
            provider_id=str(item.get("provider_id") or _infer_provider_id_from_url(b_url, proto)),
        )

    default_profile = str(raw.get("default_profile", ""))
    if default_profile not in profiles and profiles:
        default_profile = next(iter(profiles))

    providers: Dict[str, ProviderConfig] = {}
    for spec in DEFAULT_PROVIDER_SPECS:
        pname = str(spec["name"])
        providers[pname] = ProviderConfig(
            name=pname,
            label=str(spec["label"]),
            protocol=str(spec["protocol"]),
            base_url=str(spec["base_url"]),
            api_key=str(spec.get("api_key", "")),
            auth_required=bool(spec.get("auth_required", True)),
            models=list(spec.get("models", [])),
        )
    raw_providers = raw.get("providers") or {}
    if isinstance(raw_providers, dict):
        for pid, p_item in raw_providers.items():
            if not isinstance(p_item, dict):
                continue
            pid_str = str(pid).strip()
            if not pid_str:
                continue
            proto = str(p_item.get("protocol", "openai_compatible") or "openai_compatible").strip()
            b_url = str(p_item.get("base_url", "") or "").strip().rstrip("/")
            p_key = _expand_env_vars(str(p_item.get("api_key", "") or ""))
            local = is_local_provider(pid_str, b_url, proto)
            raw_models = p_item.get("models")
            base_models = list(providers[pid_str].models) if pid_str in providers else []
            models_list = (
                [str(m).strip() for m in raw_models if str(m).strip()]
                if isinstance(raw_models, list) and raw_models
                else base_models
            )
            providers[pid_str] = ProviderConfig(
                name=pid_str,
                label=str(p_item.get("label") or (providers[pid_str].label if pid_str in providers else pid_str)),
                protocol=proto,
                base_url=b_url or (providers[pid_str].base_url if pid_str in providers else ""),
                api_key=p_key or ("ollama" if local and not p_key else ""),
                auth_required=bool(p_item.get("auth_required", not local)),
                models=models_list,
            )

    raw_routing = raw.get("model_routing") or {}
    has_explicit_routing = isinstance(raw_routing, dict) and bool(raw_routing)

    ocr_raw = raw.get("ocr") or {}
    ocr_key = _expand_env_vars(str(ocr_raw.get("api_key", "") or ""))
    if not ocr_key:
        for prof in profiles.values():
            if "deepseek" in prof.base_url and prof.api_key:
                ocr_key = prof.api_key
                break
    if not providers["deepseek"].api_key:
        providers["deepseek"].api_key = ocr_key or os.environ.get("DEEPSEEK_API_KEY", "")
    elif not ocr_key:
        ocr_key = providers["deepseek"].api_key

    ocr = OCRConfig(
        provider=str(ocr_raw.get("provider", "deepseek_ocr")),
        base_url=str(ocr_raw.get("base_url", "https://api.deepseek.com/v1") or "").rstrip("/"),
        api_key=ocr_key,
        mode=str(ocr_raw.get("mode", "text_recognition")),
        timeout_seconds=float(ocr_raw.get("timeout_seconds", 20.0)),
    )

    priv_raw = raw.get("privacy") or {}
    raw_mode = str(priv_raw.get("default_mode", "meta_only")).lower()
    default_mode: PrivacyMode = (
        raw_mode if raw_mode in ("off", "meta_only", "full_on_demand") else "meta_only"
    )  # type: ignore[assignment]
    privacy = PrivacyConfig(
        default_mode=default_mode,
        blocked_apps=list(priv_raw.get("blocked_apps", DEFAULT_BLOCKED_APPS)),
        blocked_title_keywords=list(
            priv_raw.get("blocked_title_keywords", DEFAULT_BLOCKED_TITLE_KEYWORDS)
        ),
    )

    mem_raw = raw.get("memory") or {}
    ext_raw = mem_raw.get("extraction") or {}
    emb_raw = mem_raw.get("embedding") or {}
    emb_key = _expand_env_vars(str(emb_raw.get("api_key", "") or ""))
    if not emb_key and emb_raw.get("api_key_env"):
        emb_key = os.environ.get(str(emb_raw["api_key_env"]), "")

    ext_profile = str(ext_raw.get("profile", "fast_deepseek"))
    if ext_profile not in profiles and default_profile:
        ext_profile = default_profile

    memory = MemoryConfig(
        backend=str(mem_raw.get("backend", "sqlite")),
        sqlite_path=str(mem_raw.get("sqlite_path", "data/qimingxing_memory.db")),
        hot_message_limit=int(mem_raw.get("hot_message_limit", 10000)),
        archive_batch_size=int(mem_raw.get("archive_batch_size", 2000)),
        extraction=MemoryExtractionConfig(
            enabled=bool(ext_raw.get("enabled", True)),
            profile=ext_profile,
            batch_turns=int(ext_raw.get("batch_turns", 4)),
            idle_seconds=float(ext_raw.get("idle_seconds", 120.0)),
            persist_desktop_footprints=bool(ext_raw.get("persist_desktop_footprints", True)),
            compress_trigger_turns=int(ext_raw.get("compress_trigger_turns", 10)),
            compress_batch_turns=int(ext_raw.get("compress_batch_turns", 6)),
        ),
        embedding=EmbeddingConfig(
            provider=str(emb_raw.get("provider", "null")),
            base_url=str(emb_raw.get("base_url", "http://127.0.0.1:11434/v1") or "").rstrip("/"),
            api_key=emb_key,
            model=str(emb_raw.get("model", "bge-m3")),
            timeout_ms=int(emb_raw.get("timeout_ms", 800)),
        ),
    )

    budget_raw = raw.get("context_budget") or {}
    context_budget = ContextBudgetConfig(
        l1_profile_tokens=int(budget_raw.get("l1_profile_tokens", 400)),
        l2_summary_tokens=int(budget_raw.get("l2_summary_tokens", 350)),
        l2_recent_tokens=int(budget_raw.get("l2_recent_tokens", 2000)),
        l3_recall_tokens=int(budget_raw.get("l3_recall_tokens", 500)),
        l3_desktop_ocr_tokens=int(budget_raw.get("l3_desktop_ocr_tokens", 1500)),
        total_max_tokens=int(budget_raw.get("total_max_tokens", 6000)),
    )

    tools_raw = raw.get("tools") or {}
    ws_raw = tools_raw.get("web_search") or {}
    ws_key = _expand_env_vars(str(ws_raw.get("api_key", "") or "")) or ocr_key
    shell_raw = tools_raw.get("shell") or {}
    skills_raw = tools_raw.get("skills") or {}
    usage_raw = tools_raw.get("usage_alert") or {}
    learn_prof = str(skills_raw.get("learn_profile", ext_profile))
    if learn_prof not in profiles and default_profile:
        learn_prof = default_profile

    tools = ToolsConfig(
        enabled=bool(tools_raw.get("enabled", True)),
        max_tool_steps=int(tools_raw.get("max_tool_steps", 8)),
        approval_timeout_sec=int(tools_raw.get("approval_timeout_sec", 60)),
        web_search=WebSearchToolConfig(
            enabled=bool(ws_raw.get("enabled", True)),
            provider=str(ws_raw.get("provider", "deepseek")),
            base_url=str(ws_raw.get("base_url", "https://api.deepseek.com/v1") or "").rstrip("/"),
            api_key=ws_key,
            timeout_seconds=float(ws_raw.get("timeout_seconds", 15.0)),
            max_results=int(ws_raw.get("max_results", 5)),
            max_fetch_chars=int(ws_raw.get("max_fetch_chars", 6000)),
        ),
        shell=ShellToolConfig(
            enabled=bool(shell_raw.get("enabled", True)),
            safe_commands=list(shell_raw.get("safe_commands", DEFAULT_SAFE_SHELL_COMMANDS)),
            blocked_patterns=list(
                shell_raw.get("blocked_patterns", DEFAULT_BLOCKED_SHELL_PATTERNS)
            ),
            timeout_sec=int(shell_raw.get("timeout_sec", 30)),
            default_cwd=str(shell_raw.get("default_cwd", "~")),
            max_output_chars=int(shell_raw.get("max_output_chars", 4000)),
        ),
        skills=SkillsConfig(
            auto_learn=bool(skills_raw.get("auto_learn", True)),
            max_recalled_skills=int(skills_raw.get("max_recalled_skills", 2)),
            learn_profile=learn_prof,
        ),
        usage_alert=UsageAlertConfig(
            enabled=bool(usage_raw.get("enabled", True)),
            daily_warn_cny=float(usage_raw.get("daily_warn_cny", 5.0)),
            turn_warn_tokens=int(usage_raw.get("turn_warn_tokens", 50000)),
            hard_block=False,  # Soft alert only; hard blocking disabled by design
        ),
    )

    sched_raw = raw.get("scheduler") or {}
    summary_prof = str(sched_raw.get("summary_profile", ext_profile))
    if summary_prof not in profiles and default_profile:
        summary_prof = default_profile
    scheduler = SchedulerConfig(
        enabled=bool(sched_raw.get("enabled", True)),
        poll_interval_sec=float(sched_raw.get("poll_interval_sec", 5.0)),
        summary_profile=summary_prof,
    )

    mcp_servers: List[MCPServerConfig] = []
    for item in raw.get("mcp_servers") or []:
        if not isinstance(item, dict):
            continue
        srv_name = str(item.get("name") or "").strip()
        if not srv_name:
            continue
        raw_trans = str(item.get("transport", "stdio")).strip().lower()
        trans: Literal["stdio", "sse", "streamable_http"] = (
            raw_trans if raw_trans in ("stdio", "sse", "streamable_http") else "stdio"  # type: ignore[assignment]
        )
        raw_env = {str(k): _expand_env_vars(str(v)) for k, v in (item.get("env") or {}).items()}
        raw_headers = {str(k): _expand_env_vars(str(v)) for k, v in (item.get("headers") or {}).items()}
        raw_auth = str(item.get("auth_mode", "none") or "none").strip().lower()
        auth_mode: Literal["none", "oauth"] = "oauth" if raw_auth == "oauth" else "none"
        mcp_servers.append(
            MCPServerConfig(
                name=srv_name,
                enabled=bool(item.get("enabled", True)),
                transport=trans,
                command=str(item.get("command", "") or ""),
                args=[str(a) for a in (item.get("args") or [])],
                env=raw_env,
                url=str(item.get("url", "") or ""),
                headers=raw_headers,
                safe_tools=[str(t) for t in (item.get("safe_tools") or [])],
                auth_mode=auth_mode,
                timeout_sec=float(item.get("timeout_sec", 30.0)),
                startup_timeout_sec=float(item.get("startup_timeout_sec", 15.0)),
            )
        )

    audio_raw = raw.get("audio") or {}
    asr_raw = audio_raw.get("asr") or {}
    tts_raw = audio_raw.get("tts") or {}
    ptt_raw = audio_raw.get("ptt") or {}
    ds_fallback_key = ocr_key or os.environ.get("DEEPSEEK_API_KEY", "")
    asr_key = _expand_env_vars(str(asr_raw.get("api_key", "") or "")) or ds_fallback_key
    tts_key = _expand_env_vars(str(tts_raw.get("api_key", "") or "")) or ds_fallback_key
    audio = AudioConfig(
        enabled=bool(audio_raw.get("enabled", True)),
        asr=AudioASRConfig(
            provider=str(asr_raw.get("provider", "deepseek")),
            base_url=str(asr_raw.get("base_url", "https://api.deepseek.com/v1") or "").rstrip("/"),
            api_key=asr_key,
            model=str(asr_raw.get("model", "deepseek-asr")),
            timeout_sec=float(asr_raw.get("timeout_sec", 15.0)),
        ),
        tts=AudioTTSConfig(
            provider=str(tts_raw.get("provider", "deepseek")),
            base_url=str(tts_raw.get("base_url", "https://api.deepseek.com/v1") or "").rstrip("/"),
            api_key=tts_key,
            model=str(tts_raw.get("model", "deepseek-tts")),
            voice=str(tts_raw.get("voice", "stella")),
            sample_rate=int(tts_raw.get("sample_rate", 24000)),
            filter_markdown=bool(tts_raw.get("filter_markdown", True)),
            timeout_sec=float(tts_raw.get("timeout_sec", 20.0)),
        ),
        ptt=AudioPTTConfig(
            hotkey=str(ptt_raw.get("hotkey", "right_option") or "right_option"),
            sample_rate=int(ptt_raw.get("sample_rate", 16000)),
            max_record_sec=int(ptt_raw.get("max_record_sec", 60)),
        ),
    )

    batch_raw = raw.get("batch_context") or {}
    batch_context = BatchContextConfig(
        inbox_dir=str(batch_raw.get("inbox_dir", "~/Desktop/给启明星")),
        max_paths=int(batch_raw.get("max_paths", 200)),
        max_tree_depth=int(batch_raw.get("max_tree_depth", 3)),
    )

    godot_raw = raw.get("godot") or {}
    godot = GodotConfig(
        auto_hide_on_capture=bool(godot_raw.get("auto_hide_on_capture", True)),
        earcons_enabled=bool(godot_raw.get("earcons_enabled", True)),
        ambient_duck_db=float(godot_raw.get("ambient_duck_db", -12.0)),
    )

    shortcuts_raw = raw.get("shortcuts") or {}
    if not isinstance(shortcuts_raw, dict):
        shortcuts_raw = {}
    ptt_shortcut = str(shortcuts_raw.get("ptt_hotkey") or audio.ptt.hotkey or "right_option").strip().lower()
    if ptt_shortcut:
        audio.ptt.hotkey = ptt_shortcut
    shortcuts = ShortcutsConfig(
        ptt_hotkey=ptt_shortcut or "right_option",
        chat_input_hotkey=str(shortcuts_raw.get("chat_input_hotkey", "option+space") or "option+space").strip().lower(),
        cancel_hotkey=str(shortcuts_raw.get("cancel_hotkey", "option+esc") or "option+esc").strip().lower(),
        toggle_dock_hotkey=str(shortcuts_raw.get("toggle_dock_hotkey", "option+h") or "option+h").strip().lower(),
        settings_hotkey=str(shortcuts_raw.get("settings_hotkey", "option+k") or "option+k").strip().lower(),
    )

    def _parse_slot(slot_key: str, def_prov: str, def_model: str, def_voice: str = "") -> SlotRouteConfig:
        sraw = raw_routing.get(slot_key) if isinstance(raw_routing, dict) else None
        if not isinstance(sraw, dict):
            return SlotRouteConfig(provider=def_prov, model=def_model, voice=def_voice)
        prov_id = str(sraw.get("provider") or def_prov).strip()
        model_id = str(sraw.get("model") or def_model).strip()
        voice_id = str(sraw.get("voice") or def_voice).strip()
        if prov_id in providers and model_id and model_id not in providers[prov_id].models:
            providers[prov_id].models.append(model_id)
        return SlotRouteConfig(provider=prov_id, model=model_id, voice=voice_id)

    model_routing = ModelRoutingConfig(
        chat=_parse_slot("chat", "deepseek", "deepseek-chat"),
        vision_ocr=_parse_slot("vision_ocr", "deepseek", "deepseek-flash"),
        fast_utility=_parse_slot("fast_utility", "deepseek", "deepseek-chat"),
        asr=_parse_slot("asr", str(audio.asr.provider or "deepseek"), str(audio.asr.model or "deepseek-asr")),
        tts=_parse_slot("tts", str(audio.tts.provider or "deepseek"), str(audio.tts.model or "deepseek-tts"), str(audio.tts.voice or "stella")),
    )

    if has_explicit_routing:
        asr_prov = providers.get(model_routing.asr.provider)
        if asr_prov is not None:
            audio.asr.provider = asr_prov.name
            audio.asr.base_url = asr_prov.base_url.rstrip("/")
            audio.asr.api_key = asr_prov.api_key or audio.asr.api_key
            if model_routing.asr.model:
                audio.asr.model = model_routing.asr.model
        tts_prov = providers.get(model_routing.tts.provider)
        if tts_prov is not None:
            audio.tts.provider = tts_prov.name
            audio.tts.base_url = tts_prov.base_url.rstrip("/")
            audio.tts.api_key = tts_prov.api_key or audio.tts.api_key
            if model_routing.tts.model:
                audio.tts.model = model_routing.tts.model
            if model_routing.tts.voice:
                audio.tts.voice = model_routing.tts.voice
        vis_prov = providers.get(model_routing.vision_ocr.provider)
        if vis_prov is not None and "ocr" in (model_routing.vision_ocr.model or "").lower():
            ocr.base_url = vis_prov.base_url.rstrip("/")
            ocr.api_key = vis_prov.api_key or ocr.api_key
        chat_prov = providers.get(model_routing.chat.provider)
        if chat_prov is not None and model_routing.chat.model:
            local = (not chat_prov.auth_required) or is_local_provider(chat_prov.name, chat_prov.base_url, chat_prov.protocol)
            lower_cm = model_routing.chat.model.lower()
            routed_prof = ProfileConfig(
                name="routed_chat",
                label=f"{chat_prov.label} · {model_routing.chat.model}",
                provider=chat_prov.protocol,
                provider_id=chat_prov.name,
                model=model_routing.chat.model,
                base_url=chat_prov.base_url.rstrip("/"),
                api_key=chat_prov.api_key or ("ollama" if local and chat_prov.protocol == "openai_compatible" else ""),
                supports_vision=any(tok in lower_cm for tok in ("vl", "vision", "gpt-4o", "flash", "claude-3")),
                slot="chat",
            )
            profiles["routed_chat"] = routed_prof
            if raw.get("default_profile") in (None, "", "routed_chat"):
                default_profile = "routed_chat"

    return AppConfig(
        server=server,
        default_profile=default_profile,
        profiles=profiles,
        providers=providers,
        model_routing=model_routing,
        has_explicit_routing=has_explicit_routing,
        ocr=ocr,
        privacy=privacy,
        memory=memory,
        context_budget=context_budget,
        tools=tools,
        scheduler=scheduler,
        mcp_servers=mcp_servers,
        audio=audio,
        batch_context=batch_context,
        godot=godot,
        shortcuts=shortcuts,
    )


def mask_secret(secret: str) -> str:
    """Return a non-reversible masked preview of a secret key."""
    s = (secret or "").strip()
    if not s:
        return ""
    if len(s) <= 8:
        return "****"
    return f"{s[:4]}****{s[-4:]}"


def _is_masked_placeholder(value: str) -> bool:
    """Return True if a submitted secret looks like a redacted/masked placeholder."""
    s = (value or "").strip()
    if not s:
        return True
    return "****" in s or "***REDACTED***" in s


def get_sanitized_settings(cfg: AppConfig) -> Dict[str, Any]:
    """Export safe settings dictionary (API keys masked, never raw secrets)."""
    shared_ds_key = ""
    for p in cfg.profiles.values():
        if "deepseek" in (p.base_url or "").lower() and p.api_key:
            shared_ds_key = p.api_key
            break
    if not shared_ds_key:
        shared_ds_key = (
            cfg.ocr.api_key
            or cfg.tools.web_search.api_key
            or cfg.audio.asr.api_key
            or cfg.audio.tts.api_key
        )

    return {
        "default_profile": cfg.default_profile,
        "shared_deepseek_key": {
            "has_key": bool(shared_ds_key),
            "masked": mask_secret(shared_ds_key),
        },
        "profiles": {
            name: {
                "name": p.name,
                "label": p.label,
                "provider": p.provider,
                "model": p.model,
                "base_url": p.base_url,
                "supports_vision": p.supports_vision,
                "has_key": bool(p.api_key),
                "key_masked": mask_secret(p.api_key),
            }
            for name, p in cfg.profiles.items()
        },
        "ocr": {
            "provider": cfg.ocr.provider,
            "base_url": cfg.ocr.base_url,
            "mode": cfg.ocr.mode,
            "has_key": bool(cfg.ocr.api_key),
            "key_masked": mask_secret(cfg.ocr.api_key),
        },
        "web_search": {
            "enabled": cfg.tools.web_search.enabled,
            "provider": cfg.tools.web_search.provider,
            "base_url": cfg.tools.web_search.base_url,
            "has_key": bool(cfg.tools.web_search.api_key),
            "key_masked": mask_secret(cfg.tools.web_search.api_key),
        },
        "audio": {
            "enabled": cfg.audio.enabled,
            "asr": {
                "provider": cfg.audio.asr.provider,
                "base_url": cfg.audio.asr.base_url,
                "model": cfg.audio.asr.model,
                "has_key": bool(cfg.audio.asr.api_key),
                "key_masked": mask_secret(cfg.audio.asr.api_key),
            },
            "tts": {
                "provider": cfg.audio.tts.provider,
                "base_url": cfg.audio.tts.base_url,
                "model": cfg.audio.tts.model,
                "voice": cfg.audio.tts.voice,
                "sample_rate": cfg.audio.tts.sample_rate,
                "has_key": bool(cfg.audio.tts.api_key),
                "key_masked": mask_secret(cfg.audio.tts.api_key),
            },
            "ptt": {
                "hotkey": cfg.audio.ptt.hotkey,
                "sample_rate": cfg.audio.ptt.sample_rate,
                "max_record_sec": cfg.audio.ptt.max_record_sec,
            },
        },
        "privacy": {
            "default_mode": cfg.privacy.default_mode,
        },
        "batch_context": {
            "inbox_dir": cfg.batch_context.inbox_dir,
            "max_paths": cfg.batch_context.max_paths,
            "max_tree_depth": cfg.batch_context.max_tree_depth,
        },
        "godot": {
            "auto_hide_on_capture": cfg.godot.auto_hide_on_capture,
            "earcons_enabled": cfg.godot.earcons_enabled,
            "ambient_duck_db": cfg.godot.ambient_duck_db,
        },
        "shortcuts": {
            "ptt_hotkey": cfg.shortcuts.ptt_hotkey,
            "chat_input_hotkey": cfg.shortcuts.chat_input_hotkey,
            "cancel_hotkey": cfg.shortcuts.cancel_hotkey,
            "toggle_dock_hotkey": cfg.shortcuts.toggle_dock_hotkey,
            "settings_hotkey": cfg.shortcuts.settings_hotkey,
        },
        "providers": {
            pname: {
                "name": prov.name,
                "label": prov.label,
                "protocol": prov.protocol,
                "base_url": prov.base_url,
                "auth_required": prov.auth_required,
                "has_key": bool(prov.api_key and prov.auth_required),
                "key_masked": mask_secret(prov.api_key) if (prov.auth_required and prov.api_key) else "",
                "models": list(prov.models),
            }
            for pname, prov in cfg.providers.items()
        },
        "model_routing": {
            "chat": {"provider": cfg.model_routing.chat.provider, "model": cfg.model_routing.chat.model},
            "vision_ocr": {
                "provider": cfg.model_routing.vision_ocr.provider,
                "model": cfg.model_routing.vision_ocr.model,
            },
            "fast_utility": {
                "provider": cfg.model_routing.fast_utility.provider,
                "model": cfg.model_routing.fast_utility.model,
            },
            "asr": {"provider": cfg.model_routing.asr.provider, "model": cfg.model_routing.asr.model},
            "tts": {
                "provider": cfg.model_routing.tts.provider,
                "model": cfg.model_routing.tts.model,
                "voice": cfg.model_routing.tts.voice,
            },
        },
    }


def resolve_writable_config_path(explicit_path: Optional[Path] = None) -> Path:
    if explicit_path is not None:
        return explicit_path
    env_path = os.environ.get("QIMINGXING_CONFIG")
    if env_path:
        return Path(env_path)
    return _REPO_ROOT / "config.yaml"


def _load_raw_yaml_for_edit(target: Path) -> Dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    raw: Dict[str, Any] = {}
    if target.exists():
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    elif (_REPO_ROOT / "config.example.yaml").exists():
        raw = yaml.safe_load((_REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8")) or {}
    return raw if isinstance(raw, dict) else {}


def _write_yaml_atomic_0600(raw: Dict[str, Any], target: Path) -> AppConfig:
    tmp_path = target.with_suffix(".yaml.tmp")
    dumped = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    tmp_path.write_text(dumped, encoding="utf-8")
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return load_config(target)


def get_sanitized_mcp_servers(cfg: AppConfig) -> List[Dict[str, Any]]:
    """Export configured MCP servers with header/env secrets masked cleanly."""
    items: List[Dict[str, Any]] = []
    for srv in cfg.mcp_servers:
        items.append(
            {
                "name": srv.name,
                "enabled": srv.enabled,
                "transport": srv.transport,
                "command": srv.command,
                "args": list(srv.args),
                "env": {k: mask_secret(v) for k, v in srv.env.items()},
                "url": srv.url,
                "headers": {k: mask_secret(v) for k, v in srv.headers.items()},
                "safe_tools": list(srv.safe_tools),
                "auth_mode": srv.auth_mode,
                "timeout_sec": srv.timeout_sec,
                "startup_timeout_sec": srv.startup_timeout_sec,
            }
        )
    return items


def upsert_mcp_server_in_yaml(
    server_data: Dict[str, Any],
    *,
    config_path: Optional[Path] = None,
) -> AppConfig:
    """Create or update an MCP server definition in `config.yaml` (0600)."""
    name = str(server_data.get("name") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", name):
        raise ValueError("MCP 名称仅支持 1–48 位字母、数字、下划线或短横线")

    transport = str(server_data.get("transport") or "streamable_http").strip().lower()
    if transport not in ("stdio", "sse", "streamable_http"):
        raise ValueError(f"不支持的 transport: {transport}")

    auth_mode = str(server_data.get("auth_mode") or "none").strip().lower()
    if auth_mode not in ("none", "oauth"):
        raise ValueError(f"不支持的 auth_mode: {auth_mode}")

    url = str(server_data.get("url") or "").strip()
    command = str(server_data.get("command") or "").strip()
    if transport in ("sse", "streamable_http"):
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError("远程 MCP 必须填写以 http:// 或 https:// 开头的 URL")
    elif not command:
        raise ValueError("stdio 模式必须填写本地启动命令 command")

    raw_args = server_data.get("args")
    if isinstance(raw_args, str):
        args = [a for a in raw_args.split() if a]
    elif isinstance(raw_args, list):
        args = [str(a).strip() for a in raw_args if str(a).strip()]
    else:
        args = []

    raw_safe = server_data.get("safe_tools")
    if isinstance(raw_safe, str):
        safe_tools = [t.strip() for t in re.split(r"[,;\s]+", raw_safe) if t.strip()]
    elif isinstance(raw_safe, list):
        safe_tools = [str(t).strip() for t in raw_safe if str(t).strip()]
    else:
        safe_tools = []

    target = resolve_writable_config_path(config_path)
    raw = _load_raw_yaml_for_edit(target)
    servers_raw = raw.setdefault("mcp_servers", [])
    if not isinstance(servers_raw, list):
        servers_raw = []
        raw["mcp_servers"] = servers_raw

    existing: Dict[str, Any] = {}
    existing_idx = -1
    for idx, entry in enumerate(servers_raw):
        if isinstance(entry, dict) and str(entry.get("name") or "").strip() == name:
            existing = entry
            existing_idx = idx
            break

    def _merge_kv(incoming: Any, prev_map: Any) -> Dict[str, str]:
        prev_dict = prev_map if isinstance(prev_map, dict) else {}
        if not isinstance(incoming, dict):
            return {str(k): str(v) for k, v in prev_dict.items()}
        out: Dict[str, str] = {}
        for k_raw, v_raw in incoming.items():
            k = str(k_raw).strip()
            v = str(v_raw or "").strip()
            if not k:
                continue
            if _is_masked_placeholder(v):
                if k in prev_dict and str(prev_dict[k]).strip():
                    out[k] = str(prev_dict[k])
            else:
                out[k] = v
        return out

    new_entry: Dict[str, Any] = {
        "name": name,
        "enabled": bool(server_data.get("enabled", True)),
        "transport": transport,
        "auth_mode": auth_mode,
        "url": url if transport in ("sse", "streamable_http") else "",
        "command": command if transport == "stdio" else "",
        "args": args if transport == "stdio" else [],
        "safe_tools": safe_tools,
        "headers": _merge_kv(server_data.get("headers"), existing.get("headers")),
        "env": _merge_kv(server_data.get("env"), existing.get("env")),
        "timeout_sec": float(server_data.get("timeout_sec") or existing.get("timeout_sec") or 30.0),
        "startup_timeout_sec": float(
            server_data.get("startup_timeout_sec") or existing.get("startup_timeout_sec") or 20.0
        ),
    }

    if existing_idx >= 0:
        servers_raw[existing_idx] = new_entry
    else:
        servers_raw.append(new_entry)
    return _write_yaml_atomic_0600(raw, target)


def delete_mcp_server_in_yaml(
    server_name: str,
    *,
    config_path: Optional[Path] = None,
) -> AppConfig:
    """Remove an MCP server definition from `config.yaml` (0600)."""
    target_name = str(server_name or "").strip()
    target = resolve_writable_config_path(config_path)
    raw = _load_raw_yaml_for_edit(target)
    servers_raw = raw.get("mcp_servers")
    if isinstance(servers_raw, list):
        raw["mcp_servers"] = [
            entry
            for entry in servers_raw
            if not (isinstance(entry, dict) and str(entry.get("name") or "").strip() == target_name)
        ]
    return _write_yaml_atomic_0600(raw, target)


def save_settings_to_yaml(
    updates: Dict[str, Any],
    *,
    config_path: Optional[Path] = None,
) -> AppConfig:
    """Safely update user config YAML (`config.yaml`) with 0600 permissions and reload AppConfig."""
    target = resolve_writable_config_path(config_path)
    raw = _load_raw_yaml_for_edit(target)

    profiles_raw = raw.setdefault("profiles", {})
    if not isinstance(profiles_raw, dict):
        profiles_raw = {}
        raw["profiles"] = profiles_raw

    # 1. Shared DeepSeek key shortcut (updates DeepSeek profiles + OCR + web_search + audio)
    shared_ds_key = str(updates.get("shared_deepseek_api_key") or "").strip()
    if shared_ds_key and not _is_masked_placeholder(shared_ds_key):
        for pname, pdict in profiles_raw.items():
            if isinstance(pdict, dict):
                base_u = str(pdict.get("base_url") or "").lower()
                prov = str(pdict.get("provider") or "").lower()
                if prov != "mock" and ("deepseek" in base_u or "deepseek" in str(pname).lower()):
                    pdict["api_key"] = shared_ds_key
        ocr_sec = raw.setdefault("ocr", {})
        if isinstance(ocr_sec, dict):
            ocr_sec["api_key"] = shared_ds_key
        tools_sec = raw.setdefault("tools", {})
        if isinstance(tools_sec, dict):
            ws_sec = tools_sec.setdefault("web_search", {})
            if isinstance(ws_sec, dict):
                ws_sec["api_key"] = shared_ds_key
        audio_sec = raw.setdefault("audio", {})
        if isinstance(audio_sec, dict):
            asr_sec = audio_sec.setdefault("asr", {})
            tts_sec = audio_sec.setdefault("tts", {})
            if isinstance(asr_sec, dict):
                asr_sec["api_key"] = shared_ds_key
            if isinstance(tts_sec, dict):
                tts_sec["api_key"] = shared_ds_key

    # 2. Per-profile updates (empty api_key preserves existing secret)
    prof_updates = updates.get("profiles")
    if isinstance(prof_updates, dict):
        for pname, patch in prof_updates.items():
            if not isinstance(patch, dict):
                continue
            cur = profiles_raw.get(pname)
            if not isinstance(cur, dict):
                cur = {"label": str(patch.get("label") or pname), "provider": str(patch.get("provider") or "openai_compatible")}
                profiles_raw[pname] = cur
            for field_name in ("label", "provider", "model", "base_url"):
                if field_name in patch and patch[field_name] is not None:
                    val_str = str(patch[field_name]).strip()
                    if val_str:
                        cur[field_name] = val_str
            if "supports_vision" in patch and patch["supports_vision"] is not None:
                cur["supports_vision"] = bool(patch["supports_vision"])
            new_key = str(patch.get("api_key") or "").strip()
            if new_key and not _is_masked_placeholder(new_key):
                cur["api_key"] = new_key

    # 3. Active default profile
    req_def = str(updates.get("default_profile") or "").strip()
    if req_def and req_def in profiles_raw:
        raw["default_profile"] = req_def

    # 4. Audio settings
    audio_up = updates.get("audio")
    if isinstance(audio_up, dict):
        audio_sec = raw.setdefault("audio", {})
        if not isinstance(audio_sec, dict):
            audio_sec = {}
            raw["audio"] = audio_sec
        if "enabled" in audio_up:
            audio_sec["enabled"] = bool(audio_up["enabled"])
        for sub_key in ("asr", "tts", "ptt"):
            sub_up = audio_up.get(sub_key)
            if isinstance(sub_up, dict):
                sub_sec = audio_sec.setdefault(sub_key, {})
                if not isinstance(sub_sec, dict):
                    sub_sec = {}
                    audio_sec[sub_key] = sub_sec
                for k, v in sub_up.items():
                    if k == "api_key":
                        kval = str(v or "").strip()
                        if kval and not _is_masked_placeholder(kval):
                            sub_sec["api_key"] = kval
                    elif v is not None:
                        sub_sec[k] = v

    # 5. Privacy mode
    priv_up = updates.get("privacy")
    if isinstance(priv_up, dict) and "default_mode" in priv_up:
        mode_str = str(priv_up["default_mode"]).strip().lower()
        if mode_str in ("off", "meta_only", "full_on_demand"):
            priv_sec = raw.setdefault("privacy", {})
            if isinstance(priv_sec, dict):
                priv_sec["default_mode"] = mode_str

    # 6. Batch context & Godot preferences
    batch_up = updates.get("batch_context")
    if isinstance(batch_up, dict):
        batch_sec = raw.setdefault("batch_context", {})
        if isinstance(batch_sec, dict):
            if "inbox_dir" in batch_up and str(batch_up["inbox_dir"]).strip():
                batch_sec["inbox_dir"] = str(batch_up["inbox_dir"]).strip()
            if "max_paths" in batch_up and batch_up["max_paths"] is not None:
                batch_sec["max_paths"] = max(10, int(batch_up["max_paths"]))
            if "max_tree_depth" in batch_up and batch_up["max_tree_depth"] is not None:
                batch_sec["max_tree_depth"] = max(1, int(batch_up["max_tree_depth"]))

    godot_up = updates.get("godot")
    if isinstance(godot_up, dict):
        godot_sec = raw.setdefault("godot", {})
        if isinstance(godot_sec, dict):
            for k in ("auto_hide_on_capture", "earcons_enabled"):
                if k in godot_up:
                    godot_sec[k] = bool(godot_up[k])
            if "ambient_duck_db" in godot_up and godot_up["ambient_duck_db"] is not None:
                godot_sec["ambient_duck_db"] = float(godot_up["ambient_duck_db"])

    shortcuts_up = updates.get("shortcuts")
    if isinstance(shortcuts_up, dict):
        sc_sec = raw.setdefault("shortcuts", {})
        if isinstance(sc_sec, dict):
            for k in (
                "ptt_hotkey",
                "chat_input_hotkey",
                "cancel_hotkey",
                "toggle_dock_hotkey",
                "settings_hotkey",
            ):
                if k in shortcuts_up and shortcuts_up[k] is not None:
                    sc_sec[k] = str(shortcuts_up[k]).strip().lower()
            if "ptt_hotkey" in sc_sec and sc_sec["ptt_hotkey"]:
                audio_sec = raw.setdefault("audio", {})
                if isinstance(audio_sec, dict):
                    ptt_sec = audio_sec.setdefault("ptt", {})
                    if isinstance(ptt_sec, dict):
                        ptt_sec["hotkey"] = sc_sec["ptt_hotkey"]
    elif isinstance(audio_up, dict) and isinstance(audio_up.get("ptt"), dict):
        ptt_hk = audio_up["ptt"].get("hotkey")
        if ptt_hk is not None and str(ptt_hk).strip():
            sc_sec = raw.setdefault("shortcuts", {})
            if isinstance(sc_sec, dict):
                sc_sec["ptt_hotkey"] = str(ptt_hk).strip().lower()

    # 7. Providers pool & 5-slot model routing (preserving masked placeholders & 0600 secrets)
    prov_updates = updates.get("providers")
    if isinstance(prov_updates, dict) or isinstance(updates.get("model_routing"), dict):
        provs_raw = raw.setdefault("providers", {})
        if not isinstance(provs_raw, dict):
            provs_raw = {}
            raw["providers"] = provs_raw
        for spec in DEFAULT_PROVIDER_SPECS:
            sp_name = str(spec["name"])
            if sp_name not in provs_raw:
                provs_raw[sp_name] = {
                    "label": str(spec["label"]),
                    "protocol": str(spec["protocol"]),
                    "base_url": str(spec["base_url"]),
                    "auth_required": bool(spec.get("auth_required", True)),
                    "models": list(spec.get("models", [])),
                }
                if sp_name == "local_ollama":
                    provs_raw[sp_name]["api_key"] = "ollama"
                elif sp_name == "deepseek":
                    # Preserve legacy deepseek key when initializing providers block
                    legacy_ocr_k = str((raw.get("ocr") or {}).get("api_key") or "")
                    if legacy_ocr_k:
                        provs_raw[sp_name]["api_key"] = legacy_ocr_k

        if isinstance(prov_updates, dict):
            for pid_raw, patch in prov_updates.items():
                pid = str(pid_raw or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", pid) or not isinstance(patch, dict):
                    continue
                if bool(patch.get("delete")):
                    provs_raw.pop(pid, None)
                    continue
                cur_p = provs_raw.get(pid)
                if not isinstance(cur_p, dict):
                    cur_p = {"label": pid, "protocol": "openai_compatible"}
                    provs_raw[pid] = cur_p
                for fk in ("label", "protocol", "base_url"):
                    if fk in patch and patch[fk] is not None:
                        v_s = str(patch[fk]).strip()
                        if v_s:
                            cur_p[fk] = v_s.rstrip("/") if fk == "base_url" else v_s
                local_p = is_local_provider(
                    pid, str(cur_p.get("base_url") or ""), str(cur_p.get("protocol") or "")
                )
                cur_p["auth_required"] = bool(patch.get("auth_required", not local_p))
                if isinstance(patch.get("models"), list):
                    cur_p["models"] = [str(m).strip() for m in patch["models"] if str(m).strip()]
                new_pk = str(patch.get("api_key") or "").strip()
                if new_pk and not _is_masked_placeholder(new_pk):
                    cur_p["api_key"] = new_pk
                    if pid == "deepseek":
                        for pname, pdict in profiles_raw.items():
                            if isinstance(pdict, dict) and "deepseek" in str(pdict.get("base_url") or "").lower():
                                pdict["api_key"] = new_pk
                        if isinstance(raw.get("ocr"), dict):
                            raw["ocr"]["api_key"] = new_pk

    if shared_ds_key and not _is_masked_placeholder(shared_ds_key):
        provs_raw = raw.setdefault("providers", {})
        if isinstance(provs_raw, dict) and isinstance(provs_raw.get("deepseek"), dict):
            provs_raw["deepseek"]["api_key"] = shared_ds_key

    routing_up = updates.get("model_routing")
    if isinstance(routing_up, dict):
        provs_raw = raw.get("providers") or {}
        mr_raw = raw.setdefault("model_routing", {})
        if not isinstance(mr_raw, dict):
            mr_raw = {}
            raw["model_routing"] = mr_raw
        for slot_k in ("chat", "vision_ocr", "fast_utility", "asr", "tts"):
            su = routing_up.get(slot_k)
            if not isinstance(su, dict):
                continue
            p_id = str(su.get("provider") or "").strip()
            m_id = str(su.get("model") or "").strip()
            entry: Dict[str, Any] = {}
            if p_id:
                entry["provider"] = p_id
            elif isinstance(mr_raw.get(slot_k), dict):
                entry["provider"] = str(mr_raw[slot_k].get("provider") or "deepseek")
            if m_id:
                entry["model"] = m_id
            elif isinstance(mr_raw.get(slot_k), dict):
                entry["model"] = str(mr_raw[slot_k].get("model") or "")
            if slot_k == "tts":
                voice_val = str(su.get("voice") or "").strip()
                if voice_val:
                    entry["voice"] = voice_val
                elif isinstance(mr_raw.get("tts"), dict) and mr_raw["tts"].get("voice"):
                    entry["voice"] = str(mr_raw["tts"]["voice"])
            mr_raw[slot_k] = entry

            # Record custom model ID onto the target provider's preset list for convenience
            if p_id and m_id and isinstance(provs_raw, dict) and isinstance(provs_raw.get(p_id), dict):
                m_list = provs_raw[p_id].setdefault("models", [])
                if isinstance(m_list, list) and m_id not in m_list:
                    m_list.append(m_id)

        # Synchronize audio & chat routed_chat profile from saved slots
        audio_sec = raw.setdefault("audio", {})
        if isinstance(audio_sec, dict):
            for a_slot in ("asr", "tts"):
                sr = mr_raw.get(a_slot)
                if isinstance(sr, dict) and sr.get("provider"):
                    ap_dict = provs_raw.get(sr["provider"], {}) if isinstance(provs_raw, dict) else {}
                    a_sub = audio_sec.setdefault(a_slot, {})
                    if isinstance(a_sub, dict):
                        a_sub["provider"] = str(sr["provider"])
                        if ap_dict.get("base_url"):
                            a_sub["base_url"] = str(ap_dict["base_url"]).rstrip("/")
                        if ap_dict.get("api_key"):
                            a_sub["api_key"] = str(ap_dict["api_key"])
                        if sr.get("model"):
                            a_sub["model"] = str(sr["model"])
                        if a_slot == "tts" and sr.get("voice"):
                            a_sub["voice"] = str(sr["voice"])

        chat_r = mr_raw.get("chat")
        if isinstance(chat_r, dict) and chat_r.get("model"):
            cp_id = str(chat_r.get("provider") or "deepseek")
            cp_dict = provs_raw.get(cp_id, {}) if isinstance(provs_raw, dict) else {}
            cm_id = str(chat_r["model"])
            cp_label = str(cp_dict.get("label") or cp_id)
            local_c = is_local_provider(cp_id, str(cp_dict.get("base_url") or ""), str(cp_dict.get("protocol") or ""))
            profiles_raw["routed_chat"] = {
                "label": f"{cp_label} · {cm_id}",
                "provider": str(cp_dict.get("protocol") or "openai_compatible"),
                "provider_id": cp_id,
                "model": cm_id,
                "base_url": str(cp_dict.get("base_url") or "https://api.deepseek.com/v1").rstrip("/"),
                "api_key": str(cp_dict.get("api_key") or ("ollama" if local_c else "")),
                "supports_vision": any(t in cm_id.lower() for t in ("vl", "vision", "gpt-4o", "flash", "claude-3")),
            }
            if not req_def:
                raw["default_profile"] = "routed_chat"

    return _write_yaml_atomic_0600(raw, target)
