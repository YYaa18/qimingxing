from __future__ import annotations

import asyncio
import os
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from brain.config import (
    AppConfig,
    ImageSearchToolConfig,
    get_sanitized_settings,
    load_config,
    save_settings_to_yaml,
)
from brain.media_cache import DownloadedMedia, MediaCache, MediaDownloadError
from brain.config import ProfileConfig
from brain.llm_router import LLMCompletionResult, LLMRouter, ToolCallRequest
from brain.tools import Tool, ToolExecutor, ToolRegistry, ToolResult, build_default_tool_registry
from brain.tools.base import ToolExecutionContext
from brain.tools.image_search import ImageHit, ImageSearchTool, TavilyImageSearchProvider


def _png_bytes(size=(64, 48), color=(20, 30, 40)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_tavily_provider_parses_images_and_preserves_source() -> None:
    png = _png_bytes()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.tavily.com/search")
        assert request.headers["authorization"] == "Bearer tvly-test"
        payload = __import__("json").loads(request.content)
        assert payload["include_images"] is True
        assert payload["include_image_descriptions"] is True
        assert payload["safe_search"] is True
        assert payload["max_results"] == 4
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Example page",
                        "url": "https://example.com/page",
                        "images": [
                            {"url": "https://cdn.example.com/a.png", "description": "A"},
                            {"url": "https://cdn.example.com/a.png", "description": "dup"},
                        ],
                    }
                ],
                "images": [
                    {"url": "https://cdn.example.com/a.png", "description": "dup"},
                    {"url": "https://cdn.example.com/b.png", "description": "B"},
                ],
            },
        )

    provider = TavilyImageSearchProvider(
        ImageSearchToolConfig(api_key="tvly-test"),
        transport=httpx.MockTransport(handler),
    )
    hits = asyncio.run(provider.search("草原", max_results=4, safe_search=True))
    assert [h.image_url for h in hits] == [
        "https://cdn.example.com/a.png",
        "https://cdn.example.com/b.png",
    ]
    assert hits[0].source_url == "https://example.com/page"
    assert hits[0].source_title == "Example page"
    assert hits[0].provider == "tavily"


def test_media_cache_downloads_and_persists_image(tmp_path, monkeypatch) -> None:
    import brain.media_cache as media_cache_module

    async def allow(_url: str) -> None:
        return None

    monkeypatch.setattr(media_cache_module, "_assert_public_url", allow)
    png = _png_bytes()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://images.example.com/a.png")
        return httpx.Response(200, headers={"content-type": "image/png"}, content=png)

    cache = MediaCache(tmp_path / "media", transport=httpx.MockTransport(handler))
    media = asyncio.run(
        cache.download(
            "https://images.example.com/a.png",
            title="草原",
            source_url="https://example.com/page",
            source_title="示例页面",
            license="CC BY-SA",
            provider="tavily",
        )
    )
    assert Path(media.local_path).is_file()
    assert media.width == 64 and media.height == 48
    assert media.mime_type == "image/png"
    assert media.source_url == "https://example.com/page"
    assert oct(os.stat(media.local_path).st_mode & 0o777) == "0o600"
    payload = media.as_media_item()
    assert payload["kind"] == "image"
    assert payload["license"] == "CC BY-SA"
    assert payload["source_title"] == "示例页面"


def test_media_cache_rejects_non_image(tmp_path, monkeypatch) -> None:
    import brain.media_cache as media_cache_module

    async def allow(_url: str) -> None:
        return None

    monkeypatch.setattr(media_cache_module, "_assert_public_url", allow)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html></html>")

    cache = MediaCache(tmp_path / "media", transport=httpx.MockTransport(handler))
    with pytest.raises(MediaDownloadError):
        asyncio.run(cache.download("https://images.example.com/a.png"))


def test_media_cache_rejects_private_and_non_https_urls() -> None:
    from brain.media_cache import _assert_public_url

    with pytest.raises(MediaDownloadError):
        asyncio.run(_assert_public_url("https://127.0.0.1/a.png"))
    with pytest.raises(MediaDownloadError):
        asyncio.run(_assert_public_url("http://example.com/a.png"))
    with pytest.raises(MediaDownloadError):
        asyncio.run(_assert_public_url("https://localhost/a.png"))


def test_image_search_tool_returns_media_metadata(tmp_path) -> None:
    png_path = tmp_path / "a.png"
    png_path.write_bytes(_png_bytes())
    media = DownloadedMedia(
        local_path=str(png_path),
        media_id="media_test",
        mime_type="image/png",
        width=64,
        height=48,
        size_bytes=png_path.stat().st_size,
        title="草原",
        source_url="https://example.com/page",
        source_title="示例页面",
        license="CC BY-SA",
        provider="tavily",
    )

    class FakeProvider:
        async def search(self, query, *, max_results, safe_search):
            return [ImageHit(
                image_url="https://images.example.com/a.png",
                title="草原",
                source_url="https://example.com/page",
                source_title="示例页面",
                provider="tavily",
            )]

    class FakeCache:
        async def download(self, url, **kwargs):
            return media

    tool = ImageSearchTool(
        ImageSearchToolConfig(api_key="tvly-test", cache_dir=str(tmp_path / "media")),
        provider=FakeProvider(),  # type: ignore[arg-type]
        cache=FakeCache(),  # type: ignore[arg-type]
    )
    ctx = ToolExecutionContext(turn_id="t", session_id="s", config=AppConfig())
    result = asyncio.run(tool.execute({"query": "草原", "count": 2}, ctx))
    assert result.ok
    assert result.metadata["media_paths"] == [str(png_path)]
    assert result.metadata["media_items"][0]["source_url"] == "https://example.com/page"
    assert result.metadata["sources"][0]["url"] == "https://example.com/page"
    assert "http" not in result.output


class _MetadataTool(Tool):
    name = "image_search"
    description = "fake"
    parameters_schema = {"type": "object", "properties": {}}
    default_risk = "safe"

    async def execute(self, arguments, ctx):
        return ToolResult(
            ok=True,
            output="已下载",
            summary="ok",
            metadata={
                "media_paths": ["/tmp/a.png"],
                "media_items": [{"media_id": "m1", "local_path": "/tmp/a.png", "kind": "image"}],
                "sources": [{"title": "s", "url": "https://example.com", "domain": "example.com"}],
            },
        )


class _OneToolRouter(LLMRouter):
    def __init__(self, config):
        super().__init__(config)
        self.steps = [
            LLMCompletionResult(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="image_search", arguments_json="{}")],
            ),
            LLMCompletionResult(content="好了"),
        ]

    async def complete_with_tools(self, messages, tools, *, profile_name=None, on_text_delta=None, **kwargs):
        res = self.steps.pop(0)
        if res.content and on_text_delta is not None:
            await on_text_delta(res.content)
        return res


def test_executor_propagates_media_metadata_to_trace() -> None:
    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="m")},
    )
    router = _OneToolRouter(cfg)
    executor = ToolExecutor(router)
    ctx = ToolExecutionContext(turn_id="t", session_id="s", config=cfg)
    result = asyncio.run(
        executor.run(
            messages=[{"role": "user", "content": "搜图"}],
            registry=ToolRegistry([_MetadataTool()]),
            ctx=ctx,
        )
    )
    assert result.trace_steps[0].media_paths == ["/tmp/a.png"]
    assert result.trace_steps[0].media_items[0]["media_id"] == "m1"
    assert result.trace_steps[0].sources[0]["url"] == "https://example.com"


def test_image_search_settings_are_masked_and_persisted(tmp_path) -> None:
    src = Path("config.example.yaml")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = load_config(cfg_path)
    assert get_sanitized_settings(cfg)["image_search"]["has_key"] is False

    fresh = save_settings_to_yaml(
        {
            "image_search": {
                "enabled": True,
                "provider": "tavily",
                "max_results": 5,
                "api_key": "tvly-test-secret-value",
            }
        },
        config_path=cfg_path,
    )
    public = get_sanitized_settings(fresh)["image_search"]
    assert public["has_key"] is True
    assert public["key_masked"] != "tvly-test-secret-value"
    assert fresh.tools.image_search.max_results == 5
    assert oct(cfg_path.stat().st_mode & 0o777) == "0o600"


def test_image_search_tool_is_registered_for_the_model() -> None:
    registry = build_default_tool_registry(AppConfig())
    names = [tool.name for tool in registry.list_tools()]
    assert "image_search" in names
    assert "web_search" in names
