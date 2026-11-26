from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from brain.config import AppConfig, ProfileConfig, SlotRouteConfig
from brain.model_config_store import ModelConfigStore
from brain.model_discovery import discover_provider_models


def _cfg() -> AppConfig:
    cfg = AppConfig(
        default_profile="deepseek_flash",
        profiles={
            "deepseek_flash": ProfileConfig(
                name="deepseek_flash",
                label="DeepSeek Flash",
                provider="openai_compatible",
                model="deepseek-flash",
                base_url="https://api.deepseek.com/v1",
                api_key="sk-test-deepseek",
                provider_id="deepseek",
            )
        },
    )
    cfg.providers["deepseek"].api_key = "sk-test-deepseek"
    cfg.model_routing.chat = SlotRouteConfig(provider="deepseek", model="deepseek-flash")
    cfg.has_explicit_routing = True
    return cfg


def test_store_migration_apply_and_manual_capabilities(tmp_path: Path) -> None:
    cfg = _cfg()
    store = ModelConfigStore(tmp_path / "models.db", tmp_path / "config.yaml")
    store.initialize(cfg)
    store.apply_to_config(cfg)

    assert cfg.providers["deepseek"].api_key == "sk-test-deepseek"
    assert cfg.default_profile == "routed_chat"
    assert cfg.profiles["routed_chat"].model == "deepseek-flash"
    assert cfg.profiles["routed_chat"].context_window >= 128_000
    assert cfg.model_routing.chat.provider == "deepseek"
    assert cfg.model_capabilities["deepseek/deepseek-flash"]["context_window"] >= 128_000

    store.apply_provider_patch({
        "custom": {
            "label": "Custom",
            "protocol": "openai_compatible",
            "base_url": "https://example.com/v1",
            "api_key": "sk-custom",
            "auth_required": True,
            "models": ["my-model"],
            "model_capabilities": {
                "my-model": {
                    "context_window": 32768,
                    "max_output_tokens": 4096,
                    "supports_tools": True,
                    "supports_vision": False,
                }
            },
        }
    })
    store.apply_routing_patch({"chat": {"provider": "custom", "model": "my-model"}})
    store.apply_to_config(cfg)
    assert cfg.default_profile == "routed_chat"
    assert cfg.profiles["routed_chat"].provider_id == "custom"
    assert cfg.profiles["routed_chat"].context_window == 32768
    assert cfg.profiles["routed_chat"].max_output_tokens == 4096
    assert cfg.model_routing.chat.provider == "custom"


def test_store_disables_legacy_deepseek_aliases(tmp_path: Path) -> None:
    cfg = _cfg()
    cfg.providers["deepseek"].models = ["deepseek-chat", "deepseek-flash"]
    cfg.model_routing.chat = SlotRouteConfig(provider="deepseek", model="deepseek-flash")
    store = ModelConfigStore(tmp_path / "models.db")
    store.initialize(cfg)
    models = {m["model_id"]: m for m in store.provider_models("deepseek")}
    assert models["deepseek-flash"]["enabled"] == 1
    assert "deepseek-chat" not in models  # disabled rows are filtered by provider_models()


@pytest.mark.asyncio
async def test_discovery_adapters_parse_capabilities() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "generativelanguage" in url:
            assert request.headers.get("x-goog-api-key") == "g-key"
            assert request.headers.get("authorization") is None
            return httpx.Response(200, json={"models": [
                {"name": "models/gemini-3-flash-preview", "displayName": "Gemini 3 Flash",
                 "inputTokenLimit": 1048576, "outputTokenLimit": 65536,
                 "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/text-embedding-004", "inputTokenLimit": 2048,
                 "outputTokenLimit": 1, "supportedGenerationMethods": ["embedContent"]},
            ]})
        if "deepseek" in url:
            return httpx.Response(200, json={"data": [
                {"id": "deepseek-flash", "name": "DeepSeek V4 Flash",
                 "context_window": 1048576, "max_output_tokens": 393216,
                 "input_modalities": ["text", "image"], "effort": {"supported_levels": ["none", "high"]}},
            ]})
        return httpx.Response(200, json={"data": [{"id": "my-model"}]})

    transport = httpx.MockTransport(handler)

    deepseek = await discover_provider_models(
        provider_id="deepseek", protocol="openai_compatible",
        base_url="https://api.deepseek.com/v1", api_key="d-key", transport=transport,
    )
    assert deepseek[0]["context_window"] == 1048576
    assert deepseek[0]["max_output_tokens"] == 393216
    assert deepseek[0]["supports_vision"] is True

    gemini = await discover_provider_models(
        provider_id="gemini", protocol="openai_compatible",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key="g-key", transport=transport,
    )
    assert len(gemini) == 1
    assert gemini[0]["model_id"] == "gemini-3-flash-preview"
    assert gemini[0]["max_output_tokens"] == 65536

    generic = await discover_provider_models(
        provider_id="openai_compatible", protocol="openai_compatible",
        base_url="https://example.com/v1", api_key="x", transport=transport,
    )
    assert generic[0]["model_id"] == "my-model"
    assert generic[0]["context_window"] >= 0


def test_fresh_models_db_seeds_providers_without_routing(tmp_path: Path) -> None:
    cfg = AppConfig()
    store = ModelConfigStore(tmp_path / "models.db")
    store.initialize(cfg)
    store.apply_to_config(cfg)

    assert store.db_path.exists()
    assert (store.db_path.stat().st_mode & 0o777) == 0o600
    assert {"deepseek", "gemini", "openai"}.issubset(cfg.providers.keys())
    assert cfg.model_routing.chat.model == ""
    assert cfg.profiles == {}
    assert store.provider_models("deepseek")
    assert store.provider("gemini") is not None


def test_catalog_sync_replaces_retired_gemini_defaults(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "models.db")
    store.initialize(AppConfig())
    with sqlite3.connect(store.db_path) as conn:
        now = 1.0
        conn.execute(
            """INSERT OR REPLACE INTO provider_models
               (provider_id, model_id, label, context_window, max_output_tokens,
                supports_tools, supports_vision, supports_cache, supports_reasoning,
                source, metadata_json, enabled, created_at, updated_at)
               VALUES('gemini','gemini-3-pro-preview','Gemini 3 Pro',1048576,65536,
                      1,1,1,1,'catalog','{}',1,?,?)""",
            (now, now),
        )
        conn.execute(
            """INSERT OR REPLACE INTO provider_models
               (provider_id, model_id, label, context_window, max_output_tokens,
                supports_tools, supports_vision, supports_cache, supports_reasoning,
                source, metadata_json, enabled, created_at, updated_at)
               VALUES('deepseek','deepseek-v4-flash','Alias',1048576,393216,
                      1,0,1,1,'catalog','{}',1,?,?)""",
            (now, now),
        )
        conn.commit()

    store.initialize(AppConfig())
    gemini = {m["model_id"] for m in store.provider_models("gemini")}
    deepseek = {m["model_id"] for m in store.provider_models("deepseek")}
    assert "gemini-4-carbon" in gemini
    assert "gemini-4-hydrogen" in gemini
    assert "gemini-3-pro-preview" not in gemini
    assert "deepseek-v4-flash" not in deepseek


def test_custom_provider_can_be_deleted_but_builtin_cannot(tmp_path: Path) -> None:
    store = ModelConfigStore(tmp_path / "models.db")
    store.initialize(AppConfig())
    store.apply_provider_patch({
        "custom_abc123": {
            "label": "Relay",
            "base_url": "https://relay.example.com/v1",
            "api_key": "sk-relay",
            "models": ["relay-chat"],
        }
    })
    assert store.provider("custom_abc123") is not None

    store.apply_provider_patch({
        "custom_abc123": {"delete": True},
        "deepseek": {"delete": True},
    })
    assert store.provider("custom_abc123") is None
    assert store.provider("deepseek") is not None


def test_provider_api_invalid_output_limits_are_clamped(tmp_path: Path) -> None:
    cfg = _cfg()
    store = ModelConfigStore(tmp_path / "models.db", tmp_path / "config.yaml")
    store.initialize(cfg)
    store.apply_provider_patch({
        "custom_clamp": {
            "label": "Clamp",
            "protocol": "openai_compatible",
            "base_url": "https://example.com/v1",
            "models": ["image-model"],
            "model_capabilities": {
                "image-model": {
                    "context_window": 1024,
                    "max_output_tokens": 2048,
                    "source": "provider_api",
                }
            },
        }
    })
    models = store.provider_models("custom_clamp")
    assert models[0]["context_window"] == 1024
    assert models[0]["max_output_tokens"] == 512
