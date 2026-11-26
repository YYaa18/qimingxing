from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from brain.config import AppConfig, ProfileConfig, ProviderConfig, SlotRouteConfig
from brain.model_catalog import ModelCapabilities, resolve_model_capabilities


class ModelStoreError(Exception):
    """Base error for business-level model configuration conflicts."""


class ModelStoreConflictError(ModelStoreError):
    """A route/slot still references the provider being deleted."""


class ModelStoreValidationError(ModelStoreError):
    """A provider/model capability value is outside the supported range."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
    provider_id    TEXT PRIMARY KEY,
    label          TEXT NOT NULL,
    protocol       TEXT NOT NULL DEFAULT 'openai_compatible',
    base_url       TEXT NOT NULL DEFAULT '',
    api_key        TEXT NOT NULL DEFAULT '',
    auth_required  INTEGER NOT NULL DEFAULT 1,
    enabled        INTEGER NOT NULL DEFAULT 1,
    is_builtin     INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_models (
    provider_id        TEXT NOT NULL,
    model_id           TEXT NOT NULL,
    label              TEXT NOT NULL DEFAULT '',
    context_window     INTEGER NOT NULL DEFAULT 0,
    max_output_tokens  INTEGER NOT NULL DEFAULT 0,
    supports_tools     INTEGER NOT NULL DEFAULT 1,
    supports_vision    INTEGER NOT NULL DEFAULT 0,
    supports_cache     INTEGER NOT NULL DEFAULT 0,
    supports_reasoning INTEGER NOT NULL DEFAULT 0,
    source             TEXT NOT NULL DEFAULT 'manual',
    metadata_json      TEXT NOT NULL DEFAULT '{}',
    last_synced_at     REAL,
    enabled            INTEGER NOT NULL DEFAULT 1,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL,
    PRIMARY KEY (provider_id, model_id),
    FOREIGN KEY (provider_id) REFERENCES providers(provider_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_provider_models_provider
    ON provider_models(provider_id, enabled);

CREATE TABLE IF NOT EXISTS model_routing (
    slot        TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    model_id    TEXT NOT NULL,
    voice       TEXT NOT NULL DEFAULT '',
    extra_json  TEXT NOT NULL DEFAULT '{}',
    updated_at  REAL NOT NULL,
    FOREIGN KEY (provider_id) REFERENCES providers(provider_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> float:
    return time.time()


def _parse_json_object(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _chmod(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# Pre-key defaults for the three built-in providers. Live discovery replaces
# these capabilities and can add models, but never removes the user's choices.
_BUILTIN_MODELS: Dict[str, List[tuple[str, str]]] = {
    "deepseek": [
        ("deepseek-flash", "DeepSeek V4 Flash"),
        ("deepseek-v4-pro", "DeepSeek V4 Pro"),
    ],
    "gemini": [
        ("gemini-4-carbon", "Gemini 4 Carbon"),
        ("gemini-4-neon", "Gemini 4 Neon"),
        ("gemini-4-hydrogen", "Gemini 4 Hydrogen"),
        ("gemini-4-argon", "Gemini 4 Argon"),
    ],
    "openai": [
        ("gpt-4o", "GPT-4o"),
        ("gpt-4o-mini", "GPT-4o mini"),
    ],
}


class ModelConfigStore:
    """SQLite source of truth for providers, per-model capabilities and routing."""

    def __init__(self, db_path: Path | str, config_path: Optional[Path | str] = None) -> None:
        self.db_path = Path(db_path)
        self.config_path = Path(config_path) if config_path else None

    # ------------------------------------------------------------------ db
    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        _chmod(self.db_path)
        for suffix in ("-wal", "-shm"):
            _chmod(Path(str(self.db_path) + suffix))
        return conn

    def initialize(self, cfg: Optional[AppConfig] = None) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()
            self._ensure_routing_schema(conn)
            empty = conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0] == 0
            if empty:
                self._seed(conn)
            migrated = conn.execute(
                "SELECT value FROM meta WHERE key='migrated_from_yaml'"
            ).fetchone()
            if cfg is not None and migrated is None:
                self._backup_config_before_migration()
                self._migrate_from_config(conn, cfg)
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('migrated_from_yaml', ?)",
                    ("1",),
                )
                conn.commit()
            # Built-ins are a fixed product surface: normalize their labels
            # and flags even when the rows were migrated from an older YAML.
            for pid, label in (
                ("deepseek", "DeepSeek"),
                ("gemini", "Google Gemini"),
                ("openai", "OpenAI"),
            ):
                conn.execute(
                    "UPDATE providers SET label=?, is_builtin=1 WHERE provider_id=?",
                    (label, pid),
                )
            # Keep the built-in catalog aligned with the models the product
            # actually offers. Discovery results and migrated/manual models are
            # never removed; only stale entries seeded by us are pruned.
            self._sync_builtin_models(conn)
            # The product only exposes DeepSeek / Gemini / OpenAI plus
            # user-added custom providers. Remove legacy built-in presets.
            conn.execute(
                "DELETE FROM model_routing WHERE provider_id IN (?, ?, ?, ?)",
                ("siliconflow", "local_ollama", "anthropic", "openai_compatible"),
            )
            conn.execute(
                "DELETE FROM providers WHERE provider_id IN (?, ?, ?, ?)",
                ("siliconflow", "local_ollama", "anthropic", "openai_compatible"),
            )
            conn.commit()
        for suffix in ("", "-wal", "-shm"):
            _chmod(Path(str(self.db_path) + suffix))

    # ------------------------------------------------------------------ schema
    def _ensure_routing_schema(self, conn: sqlite3.Connection) -> None:
        """Add extra_json and switch the routing FK to RESTRICT on old DBs."""
        cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(model_routing)")}
        fk_rows = list(conn.execute("PRAGMA foreign_key_list(model_routing)"))
        on_delete = ""
        for row in fk_rows:
            if str(row[2]) == "providers":
                on_delete = str(row[6] or "")
        if "extra_json" in cols and on_delete.upper() == "RESTRICT":
            return

        extra_expr = "extra_json" if "extra_json" in cols else "'{}'"
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("ALTER TABLE model_routing RENAME TO model_routing_old")
            conn.execute(
                """CREATE TABLE model_routing (
                       slot        TEXT PRIMARY KEY,
                       provider_id TEXT NOT NULL,
                       model_id    TEXT NOT NULL,
                       voice       TEXT NOT NULL DEFAULT '',
                       extra_json  TEXT NOT NULL DEFAULT '{}',
                       updated_at  REAL NOT NULL,
                       FOREIGN KEY (provider_id) REFERENCES providers(provider_id) ON DELETE RESTRICT
                   )"""
            )
            conn.execute(
                f"""INSERT INTO model_routing
                    (slot, provider_id, model_id, voice, extra_json, updated_at)
                    SELECT slot, provider_id, model_id, voice, {extra_expr}, updated_at
                    FROM model_routing_old"""
            )
            conn.execute("DROP TABLE model_routing_old")
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    def _backup_config_before_migration(self) -> None:
        if self.config_path is None or not self.config_path.exists():
            return
        backup = self.config_path.with_name(self.config_path.name + ".bak-models")
        if backup.exists():
            return
        tmp = backup.with_name(backup.name + ".tmp")
        try:
            shutil.copyfile(self.config_path, tmp)
            os.chmod(tmp, 0o600)
            os.replace(tmp, backup)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------ seeds
    def _seed(self, conn: sqlite3.Connection) -> None:
        providers = [
            ("deepseek", "DeepSeek", "openai_compatible", "https://api.deepseek.com/v1", 1),
            ("gemini", "Google Gemini", "openai_compatible", "https://generativelanguage.googleapis.com/v1beta/openai/", 1),
            ("openai", "OpenAI", "openai_compatible", "https://api.openai.com/v1", 1),
        ]
        now = _now()
        for pid, label, protocol, base, auth in providers:
            conn.execute(
                """INSERT OR IGNORE INTO providers
                   (provider_id, label, protocol, base_url, api_key, auth_required,
                    enabled, is_builtin, created_at, updated_at)
                   VALUES (?, ?, ?, ?, '', ?, 1, 1, ?, ?)""",
                (pid, label, protocol, base, auth, now, now),
            )
        self._sync_builtin_models(conn)

    def _sync_builtin_models(self, conn: sqlite3.Connection) -> None:
        for pid, rows in _BUILTIN_MODELS.items():
            for model_id, label in rows:
                exists = conn.execute(
                    "SELECT 1 FROM provider_models WHERE provider_id=? AND model_id=?",
                    (pid, model_id),
                ).fetchone()
                if exists:
                    continue
                caps = resolve_model_capabilities(pid, model_id)
                self._upsert_model(conn, pid, model_id, label, caps, source="catalog")
            keep = [model_id for model_id, _ in rows]
            if not keep:
                continue
            placeholders = ",".join("?" for _ in keep)
            conn.execute(
                f"""DELETE FROM provider_models
                    WHERE provider_id=? AND source='catalog'
                      AND model_id NOT IN ({placeholders})
                      AND model_id NOT IN (
                          SELECT model_id FROM model_routing WHERE provider_id=?
                      )""",
                (pid, *keep, pid),
            )

    def _migrate_from_config(self, conn: sqlite3.Connection, cfg: AppConfig) -> None:
        if not cfg.profiles and not any(
            p.api_key and p.auth_required for p in cfg.providers.values()
        ):
            # Fresh install: keep only the intentional provider seeds and wait
            # for the user's first model setup.
            return
        now = _now()
        for pid, prov in cfg.providers.items():
            conn.execute(
                """INSERT INTO providers
                   (provider_id, label, protocol, base_url, api_key, auth_required,
                    enabled, is_builtin, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
                   ON CONFLICT(provider_id) DO UPDATE SET
                     label=excluded.label,
                     protocol=excluded.protocol,
                     base_url=excluded.base_url,
                     api_key=CASE WHEN excluded.api_key != '' THEN excluded.api_key ELSE providers.api_key END,
                     auth_required=excluded.auth_required,
                     updated_at=excluded.updated_at""",
                (
                    pid,
                    prov.label or pid,
                    prov.protocol or "openai_compatible",
                    prov.base_url or "",
                    prov.api_key or "",
                    1 if prov.auth_required else 0,
                    now,
                    now,
                ),
            )
            for model_id in prov.models:
                caps = cfg.model_capabilities.get(f"{pid}/{model_id}") or {}
                if caps:
                    cap = ModelCapabilities(
                        int(caps.get("context_window") or 0),
                        int(caps.get("max_output_tokens") or 0),
                        bool(caps.get("supports_tools", True)),
                        bool(caps.get("supports_vision", False)),
                        bool(caps.get("supports_cache", False)),
                        bool(caps.get("supports_reasoning", False)),
                        str(caps.get("source") or "migrated"),
                    )
                else:
                    cap = resolve_model_capabilities(pid, model_id)
                self._upsert_model(conn, pid, model_id, model_id, cap, source="migrated")
                if pid == "deepseek" and model_id in ("deepseek-chat", "deepseek-reasoner"):
                    conn.execute(
                        "UPDATE provider_models SET enabled=0 WHERE provider_id=? AND model_id=?",
                        (pid, model_id),
                    )

        for slot in ("chat", "vision_ocr", "fast_utility", "asr", "tts"):
            route = getattr(cfg.model_routing, slot, None)
            if route is None or not route.provider or not route.model:
                continue
            prov = cfg.providers.get(route.provider)
            if prov is None:
                continue
            if prov.auth_required and not prov.api_key:
                # Fresh installs without a key should go through onboarding.
                continue
            self._ensure_model(conn, route.provider, route.model)
            conn.execute(
                """INSERT OR REPLACE INTO model_routing(slot, provider_id, model_id, voice, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (slot, route.provider, route.model, route.voice or "", now),
            )
        conn.commit()

    # ------------------------------------------------------------------ models
    def _upsert_model(
        self,
        conn: sqlite3.Connection,
        provider_id: str,
        model_id: str,
        label: str,
        caps: ModelCapabilities,
        *,
        source: str = "manual",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = _now()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        conn.execute(
            """INSERT INTO provider_models
               (provider_id, model_id, label, context_window, max_output_tokens,
                supports_tools, supports_vision, supports_cache, supports_reasoning,
                source, metadata_json, last_synced_at, enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT(provider_id, model_id) DO UPDATE SET
                 label=CASE WHEN excluded.label != '' THEN excluded.label ELSE provider_models.label END,
                 context_window=CASE WHEN provider_models.source='manual' AND excluded.source != 'manual'
                                     THEN provider_models.context_window ELSE excluded.context_window END,
                 max_output_tokens=CASE WHEN provider_models.source='manual' AND excluded.source != 'manual'
                                        THEN provider_models.max_output_tokens ELSE excluded.max_output_tokens END,
                 supports_tools=excluded.supports_tools,
                 supports_vision=excluded.supports_vision,
                 supports_cache=excluded.supports_cache,
                 supports_reasoning=excluded.supports_reasoning,
                 source=CASE WHEN provider_models.source='manual' AND excluded.source != 'manual'
                             THEN 'manual' ELSE excluded.source END,
                 metadata_json=CASE WHEN excluded.metadata_json != '{}'
                                    THEN excluded.metadata_json ELSE provider_models.metadata_json END,
                 last_synced_at=excluded.last_synced_at,
                 enabled=1,
                 updated_at=excluded.updated_at""",
            (
                provider_id,
                model_id,
                label or model_id,
                int(caps.context_window),
                int(caps.max_output_tokens),
                1 if caps.supports_tools else 0,
                1 if caps.supports_vision else 0,
                1 if caps.supports_cache else 0,
                1 if caps.supports_reasoning else 0,
                source,
                metadata_json,
                now,
                now,
                now,
            ),
        )

    def _ensure_model(self, conn: sqlite3.Connection, provider_id: str, model_id: str) -> None:
        row = conn.execute(
            "SELECT 1 FROM provider_models WHERE provider_id=? AND model_id=?",
            (provider_id, model_id),
        ).fetchone()
        if row:
            return
        caps = resolve_model_capabilities(provider_id, model_id)
        self._upsert_model(conn, provider_id, model_id, model_id, caps, source="catalog")

    # ------------------------------------------------------------------ apply
    @staticmethod
    def _apply_extra_to_profile(
        profile: ProfileConfig, extra: Dict[str, Any]
    ) -> ProfileConfig:
        temperature = extra.get("temperature")
        if isinstance(temperature, (int, float)):
            profile.temperature = max(0.0, min(2.0, float(temperature)))
        max_override = extra.get("max_output_override")
        if isinstance(max_override, (int, float)) and int(max_override) > 0:
            profile.request_max_tokens = int(max_override)
        effort = str(
            extra.get("effort") or extra.get("reasoning_effort") or ""
        ).strip().lower()
        if effort in ("", "none", "low", "medium", "high", "max", "xhigh"):
            profile.reasoning_effort = effort
        return profile

    @staticmethod
    def _is_chat_model(model_id: str) -> bool:
        lower = (model_id or "").lower()
        return not any(
            token in lower
            for token in (
                "ocr", "asr", "tts", "embedding", "whisper", "cosyvoice",
                "sensevoice", "image", "live", "audio", "computer-use",
                "deep-research", "antigravity", "transcribe", "translate",
            )
        )

    @staticmethod
    def _profile_name(provider_id: str, model_id: str) -> str:
        safe = "".join(ch if ch.isalnum() else "_" for ch in model_id.lower())
        return f"model_{provider_id}_{safe}".strip("_")

    def apply_to_config(self, cfg: AppConfig) -> AppConfig:
        with self._connect() as conn:
            provider_rows = conn.execute(
                "SELECT * FROM providers WHERE enabled=1 ORDER BY is_builtin DESC, label"
            ).fetchall()
            model_rows = conn.execute(
                "SELECT * FROM provider_models WHERE enabled=1 ORDER BY provider_id, model_id"
            ).fetchall()
            route_rows = conn.execute(
                "SELECT * FROM model_routing ORDER BY slot"
            ).fetchall()

        providers: Dict[str, ProviderConfig] = {}
        caps_map: Dict[str, Dict[str, Any]] = {}
        models_by_provider: Dict[str, List[str]] = {}
        model_labels: Dict[str, str] = {}
        for row in model_rows:
            pid = str(row["provider_id"])
            mid = str(row["model_id"])
            models_by_provider.setdefault(pid, []).append(mid)
            model_labels[f"{pid}/{mid}"] = str(row["label"] or mid)
            caps_map[f"{pid}/{mid}"] = {
                "context_window": int(row["context_window"] or 0),
                "max_output_tokens": int(row["max_output_tokens"] or 0),
                "supports_tools": bool(row["supports_tools"]),
                "supports_vision": bool(row["supports_vision"]),
                "supports_cache": bool(row["supports_cache"]),
                "supports_reasoning": bool(row["supports_reasoning"]),
                "source": str(row["source"] or "models_db"),
                "is_preview": bool(
                    _parse_json_object(row["metadata_json"]).get("is_preview")
                )
                or ("preview" in mid.lower() or "exp" in mid.lower()),
            }
        for row in provider_rows:
            pid = str(row["provider_id"])
            providers[pid] = ProviderConfig(
                name=pid,
                label=str(row["label"] or pid),
                protocol=str(row["protocol"] or "openai_compatible"),
                base_url=str(row["base_url"] or ""),
                api_key=str(row["api_key"] or ""),
                auth_required=bool(row["auth_required"]),
                models=models_by_provider.get(pid, []),
            )

        cfg.providers = providers
        cfg.model_capabilities = caps_map
        cfg.model_routing = cfg.model_routing.__class__()
        cfg.profiles = {}
        enabled_models = {
            f"{row['provider_id']}/{row['model_id']}" for row in model_rows
        }
        enabled_routes = [
            row for row in route_rows
            if f"{row['provider_id']}/{row['model_id']}" in enabled_models
        ]
        cfg.has_explicit_routing = bool(enabled_routes)
        if not enabled_routes:
            for slot_name in ("chat", "vision_ocr", "fast_utility", "asr", "tts"):
                setattr(cfg.model_routing, slot_name, SlotRouteConfig(provider="", model=""))
        for row in enabled_routes:
            slot = str(row["slot"])
            pid = str(row["provider_id"])
            mid = str(row["model_id"])
            voice = str(row["voice"] or "")
            extra = _parse_json_object(row["extra_json"])
            setattr(
                cfg.model_routing,
                slot,
                SlotRouteConfig(provider=pid, model=mid, voice=voice, extra=extra),
            )

        chat = next((r for r in enabled_routes if str(r["slot"]) == "chat"), None)
        if chat is not None:
            pid = str(chat["provider_id"])
            mid = str(chat["model_id"])
            prov = providers.get(pid)
            caps = caps_map.get(f"{pid}/{mid}", {})
            if prov is not None:
                label = model_labels.get(f"{pid}/{mid}", mid)
                extra = _parse_json_object(chat["extra_json"])
                routed_profile = ProfileConfig(
                    name="routed_chat",
                    label=f"{prov.label} · {label}",
                    provider=prov.protocol,
                    provider_id=pid,
                    model=mid,
                    base_url=prov.base_url.rstrip("/"),
                    api_key=prov.api_key,
                    supports_vision=bool(caps.get("supports_vision")),
                    context_window=int(caps.get("context_window") or 0),
                    max_output_tokens=int(caps.get("max_output_tokens") or 0),
                    capability_source=str(caps.get("source") or "models_db"),
                    slot="chat",
                )
                cfg.profiles["routed_chat"] = self._apply_extra_to_profile(
                    routed_profile, extra
                )
                cfg.default_profile = "routed_chat"
        # Expose the latest model family as switchable profiles. Older
        # generations remain selectable in settings routing, but keeping them
        # out of the quick switcher avoids a 30+ item menu after discovery.
        latest_gemini_major = 0
        for row in model_rows:
            if str(row["provider_id"]) != "gemini":
                continue
            match = re.match(r"gemini-(\d+)", str(row["model_id"]))
            if match:
                latest_gemini_major = max(latest_gemini_major, int(match.group(1)))
        routed_key = (
            f"{chat['provider_id']}/{chat['model_id']}" if chat is not None else ""
        )
        for row in model_rows:
            pid = str(row["provider_id"])
            mid = str(row["model_id"])
            if not self._is_chat_model(mid) or f"{pid}/{mid}" == routed_key:
                continue
            if pid == "gemini":
                match = re.match(r"gemini-(\d+)", mid)
                if not match or (
                    latest_gemini_major and int(match.group(1)) < latest_gemini_major
                ):
                    continue
            prov = providers.get(pid)
            if prov is None or (prov.auth_required and not prov.api_key):
                continue
            caps = caps_map.get(f"{pid}/{mid}", {})
            profile_name = self._profile_name(pid, mid)
            if profile_name not in cfg.profiles:
                cfg.profiles[profile_name] = ProfileConfig(
                    name=profile_name,
                    label=f"{prov.label} · {model_labels.get(f'{pid}/{mid}', mid)}",
                    provider=prov.protocol,
                    provider_id=pid,
                    model=mid,
                    base_url=prov.base_url.rstrip("/"),
                    api_key=prov.api_key,
                    supports_vision=bool(caps.get("supports_vision")),
                    context_window=int(caps.get("context_window") or 0),
                    max_output_tokens=int(caps.get("max_output_tokens") or 0),
                    capability_source=str(caps.get("source") or "models_db"),
                    slot="chat",
                )
        if not cfg.profiles:
            cfg.default_profile = ""
        return cfg

    # ------------------------------------------------------------------ writes
    def apply_provider_patch(self, providers_patch: Dict[str, Any]) -> None:
        if not isinstance(providers_patch, dict):
            return
        with self._connect() as conn:
            now = _now()
            for pid, patch in providers_patch.items():
                if not isinstance(patch, dict):
                    continue
                pid = str(pid)
                if patch.get("delete") is True or str(patch.get("delete")).lower() == "true":
                    # Built-ins are part of the product contract; only user
                    # providers may be removed from settings.
                    if pid.startswith("custom_"):
                        used = [
                            str(row[0])
                            for row in conn.execute(
                                "SELECT slot FROM model_routing WHERE provider_id=? ORDER BY slot",
                                (pid,),
                            ).fetchall()
                        ]
                        if used:
                            raise ModelStoreConflictError(
                                f"该服务商正被 {', '.join(used)} 槽位使用，请先切换路由后再删除"
                            )
                        conn.execute("DELETE FROM providers WHERE provider_id=?", (pid,))
                    continue
                existing = conn.execute(
                    "SELECT * FROM providers WHERE provider_id=?", (pid,)
                ).fetchone()
                label = str(patch.get("label") or (existing["label"] if existing else pid))
                protocol = str(patch.get("protocol") or (existing["protocol"] if existing else "openai_compatible"))
                base_url = str(patch.get("base_url") or (existing["base_url"] if existing else ""))
                auth = bool(patch.get("auth_required", existing["auth_required"] if existing else True))
                key = str(patch.get("api_key") or "").strip()
                if "****" in key or "REDACTED" in key:
                    key = ""
                if key:
                    conn.execute(
                        """INSERT INTO providers(provider_id,label,protocol,base_url,api_key,auth_required,enabled,is_builtin,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,1,0,?,?)
                           ON CONFLICT(provider_id) DO UPDATE SET
                             label=excluded.label, protocol=excluded.protocol,
                             base_url=excluded.base_url, api_key=excluded.api_key,
                             auth_required=excluded.auth_required, updated_at=excluded.updated_at""",
                        (pid, label, protocol, base_url, key, 1 if auth else 0, now, now),
                    )
                else:
                    conn.execute(
                        """INSERT INTO providers(provider_id,label,protocol,base_url,api_key,auth_required,enabled,is_builtin,created_at,updated_at)
                           VALUES(?,?,?,?,'',?,1,0,?,?)
                           ON CONFLICT(provider_id) DO UPDATE SET
                             label=excluded.label, protocol=excluded.protocol,
                             base_url=excluded.base_url,
                             auth_required=excluded.auth_required, updated_at=excluded.updated_at""",
                        (pid, label, protocol, base_url, 1 if auth else 0, now, now),
                    )
                models = patch.get("models")
                caps_patch = patch.get("model_capabilities") or {}
                if isinstance(models, list):
                    keep: List[str] = []
                    for mid in models:
                        mid = str(mid).strip()
                        if not mid:
                            continue
                        keep.append(mid)
                        cap_data = caps_patch.get(mid) if isinstance(caps_patch, dict) else None
                        if isinstance(cap_data, dict):
                            try:
                                context_window = int(cap_data.get("context_window") or 0)
                                max_output = int(cap_data.get("max_output_tokens") or 0)
                            except (TypeError, ValueError) as exc:
                                raise ModelStoreValidationError(
                                    f"模型 {mid} 的上下文/输出必须是整数"
                                ) from exc
                            if context_window and not (1024 <= context_window <= 10_000_000):
                                raise ModelStoreValidationError(
                                    f"模型 {mid} 的上下文窗口必须在 1,024–10,000,000 之间"
                                )
                            if max_output and not (0 < max_output <= 10_000_000):
                                raise ModelStoreValidationError(
                                    f"模型 {mid} 的最大输出必须在 1–10,000,000 之间"
                                )
                            source = str(cap_data.get("source") or "manual")
                            if context_window and max_output and max_output >= context_window:
                                if source == "manual":
                                    raise ModelStoreValidationError(
                                        f"模型 {mid} 的最大输出必须小于上下文窗口"
                                    )
                                # Provider discovery occasionally reports
                                # outputTokenLimit == context limit (Gemini image
                                # models). Clamp discovered values instead of
                                # blocking every settings save.
                                max_output = max(1, context_window // 2)
                            cap = ModelCapabilities(
                                context_window,
                                max_output,
                                bool(cap_data.get("supports_tools", True)),
                                bool(cap_data.get("supports_vision", False)),
                                bool(cap_data.get("supports_cache", False)),
                                bool(cap_data.get("supports_reasoning", False)),
                                source,
                            )
                            metadata = (
                                {"is_preview": True}
                                if cap_data.get("is_preview")
                                else None
                            )
                            self._upsert_model(
                                conn,
                                pid,
                                mid,
                                mid,
                                cap,
                                source=str(cap_data.get("source") or "manual"),
                                metadata=metadata,
                            )
                        else:
                            self._ensure_model(conn, pid, mid)
                    if keep:
                        placeholders = ",".join("?" for _ in keep)
                        conn.execute(
                            f"UPDATE provider_models SET enabled=0 WHERE provider_id=? AND model_id NOT IN ({placeholders})",
                            (pid, *keep),
                        )
            conn.commit()

    def apply_routing_patch(self, routing_patch: Dict[str, Any]) -> None:
        if not isinstance(routing_patch, dict):
            return
        with self._connect() as conn:
            now = _now()
            for slot, route in routing_patch.items():
                slot = str(slot)
                if not isinstance(route, dict):
                    continue
                pid = str(route.get("provider") or "").strip()
                mid = str(route.get("model") or "").strip()
                if not pid or not mid:
                    conn.execute("DELETE FROM model_routing WHERE slot=?", (slot,))
                    continue
                existing = conn.execute(
                    "SELECT voice, extra_json FROM model_routing WHERE slot=?", (slot,)
                ).fetchone()
                old_extra: Dict[str, Any] = {}
                if existing is not None:
                    try:
                        parsed = json.loads(str(existing["extra_json"] or "{}"))
                        if isinstance(parsed, dict):
                            old_extra = parsed
                    except json.JSONDecodeError:
                        old_extra = {}
                raw_extra = route.get("extra")
                if raw_extra is None:
                    raw_extra = route.get("extra_json")
                if isinstance(raw_extra, str):
                    try:
                        raw_extra = json.loads(raw_extra) if raw_extra.strip() else {}
                    except json.JSONDecodeError:
                        raw_extra = {}
                new_extra = dict(raw_extra) if isinstance(raw_extra, dict) else {}
                extra = {**old_extra, **new_extra}
                voice = str(
                    route.get("voice")
                    or extra.get("voice")
                    or (existing["voice"] if existing is not None else "")
                    or ""
                )
                if voice:
                    extra["voice"] = voice
                self._ensure_model(conn, pid, mid)
                conn.execute(
                    """INSERT OR REPLACE INTO model_routing
                       (slot, provider_id, model_id, voice, extra_json, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (slot, pid, mid, voice, json.dumps(extra, ensure_ascii=False), now),
                )
            conn.commit()

    def upsert_discovered(self, provider_id: str, models: Iterable[Dict[str, Any]]) -> int:
        count = 0
        with self._connect() as conn:
            now = _now()
            seen: List[str] = []
            for item in models:
                mid = str(item.get("model_id") or item.get("id") or "").strip()
                if not mid:
                    continue
                seen.append(mid)
                cap = ModelCapabilities(
                    int(item.get("context_window") or 0),
                    int(item.get("max_output_tokens") or 0),
                    bool(item.get("supports_tools", True)),
                    bool(item.get("supports_vision", False)),
                    bool(item.get("supports_cache", False)),
                    bool(item.get("supports_reasoning", False)),
                    "provider_api",
                )
                metadata = {"is_preview": True} if item.get("is_preview") else None
                self._upsert_model(
                    conn,
                    provider_id,
                    mid,
                    str(item.get("label") or mid),
                    cap,
                    source="provider_api",
                    metadata=metadata,
                )
                count += 1
            if seen:
                # Only retire models that were previously discovered from the
                # provider API. Seeded/migrated audio/OCR models may not appear
                # in the chat model list and must remain available.
                placeholders = ",".join("?" for _ in seen)
                conn.execute(
                    "UPDATE provider_models SET enabled=0 "
                    "WHERE provider_id=? AND source='provider_api' "
                    f"AND model_id NOT IN ({placeholders})",
                    (provider_id, *seen),
                )
            conn.commit()
        return count

    def provider(self, provider_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM providers WHERE provider_id=? AND enabled=1",
                (provider_id,),
            ).fetchone()
        return dict(row) if row else None

    def provider_models(self, provider_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM provider_models WHERE provider_id=? AND enabled=1 ORDER BY model_id",
                (provider_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def routing(self) -> Dict[str, Dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM model_routing").fetchall()
        return {
            str(row["slot"]): {
                "provider": str(row["provider_id"]),
                "model": str(row["model_id"]),
                "voice": str(row["voice"] or ""),
                "extra": _parse_json_object(row["extra_json"]),
            }
            for row in rows
        }
