from __future__ import annotations

from brain.config_defaults import (
    DEFAULT_BLOCKED_APPS,
    DEFAULT_BLOCKED_SHELL_PATTERNS,
    DEFAULT_BLOCKED_TITLE_KEYWORDS,
    DEFAULT_SAFE_SHELL_COMMANDS,
)
from brain.config_models import (
    DEFAULT_PROVIDER_SPECS,
    _infer_provider_id_from_url,
    ProviderConfig,
    SlotRouteConfig,
    ModelRoutingConfig,
    ProfileConfig,
    ServerConfig,
    OCRConfig,
    PrivacyConfig,
    MemoryExtractionConfig,
    EmbeddingConfig,
    MemoryConfig,
    ContextBudgetConfig,
    WebSearchToolConfig,
    ImageSearchToolConfig,
    ShellToolConfig,
    SkillsConfig,
    UsageAlertConfig,
    ToolsConfig,
    SchedulerConfig,
    MCPServerConfig,
    AudioASRConfig,
    AudioTTSConfig,
    AudioPTTConfig,
    AudioConfig,
    BatchContextConfig,
    GodotConfig,
    UIConfig,
    BackgroundTaskConfig,
    BrowserAutomationConfig,
    ShortcutsConfig,
    DeepSeekConfig,
    GeminiConfig,
    AppConfig,
    is_local_provider,
)

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlparse

import yaml

from protocol.models import PrivacyMode, ProfileInfo

from brain.config_io import (
    _expand_env_vars,
    resolve_config_path,
    load_config,
    mask_secret,
    _is_masked_placeholder,
    get_sanitized_settings,
    resolve_writable_config_path,
    _load_raw_yaml_for_edit,
    _write_yaml_atomic_0600,
    get_sanitized_mcp_servers,
    upsert_mcp_server_in_yaml,
    delete_mcp_server_in_yaml,
    save_settings_to_yaml,
)
