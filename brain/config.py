from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

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
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    default_profile: str = "mock_local"
    profiles: Dict[str, ProfileConfig] = field(default_factory=dict)
    ocr: OCRConfig = field(default_factory=OCRConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    _secrets: List[str] = field(default_factory=list, repr=False)

    def collect_secrets(self) -> List[str]:
        secrets = [p.api_key for p in self.profiles.values() if p.api_key]
        if self.ocr.api_key:
            secrets.append(self.ocr.api_key)
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
        profiles[name] = ProfileConfig(
            name=name,
            label=str(item.get("label", name)),
            provider=str(item.get("provider", "openai_compatible")),
            model=str(item.get("model", "")),
            base_url=str(item.get("base_url", "") or "").rstrip("/"),
            api_key=expanded_key,
            temperature=float(item.get("temperature", 0.7)),
            timeout_seconds=float(item.get("timeout_seconds", 30.0)),
            supports_vision=bool(item.get("supports_vision", False)),
        )

    default_profile = str(raw.get("default_profile", ""))
    if default_profile not in profiles and profiles:
        default_profile = next(iter(profiles))

    ocr_raw = raw.get("ocr") or {}
    ocr_key = _expand_env_vars(str(ocr_raw.get("api_key", "") or ""))
    if not ocr_key:
        # Fallback to any configured DeepSeek profile key if available
        for prof in profiles.values():
            if "deepseek" in prof.base_url and prof.api_key:
                ocr_key = prof.api_key
                break
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

    return AppConfig(
        server=server,
        default_profile=default_profile,
        profiles=profiles,
        ocr=ocr,
        privacy=privacy,
    )
