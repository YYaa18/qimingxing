from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx

from brain.config import ImageSearchToolConfig
from brain.media_cache import MediaCache, MediaDownloadError
from brain.tools.base import Tool, ToolExecutionContext, ToolResult
from protocol.models import RiskLevel

logger = logging.getLogger("qimingxing.tools.image_search")


@dataclass
class ImageHit:
    image_url: str
    title: str = ""
    source_url: str = ""
    source_title: str = ""
    license: str = ""
    attribution: str = ""
    provider: str = ""
    thumbnail_url: str = ""


class TavilyImageSearchProvider:
    """Tavily /search with include_images; Tavily has no dedicated image endpoint."""

    def __init__(
        self,
        config: ImageSearchToolConfig,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.config = config
        self._transport = transport

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        safe_search: bool,
    ) -> List[ImageHit]:
        if not self.config.api_key:
            raise RuntimeError("图片搜索缺少 Tavily API Key。")
        endpoint = f"{self.config.base_url.rstrip('/')}/search"
        payload = {
            "query": query,
            "search_depth": "basic",
            "include_images": True,
            "include_image_descriptions": True,
            "max_results": max(1, min(20, max_results)),
            "safe_search": bool(safe_search),
        }
        timeout = httpx.Timeout(self.config.timeout_seconds, connect=8.0)
        kwargs: Dict[str, Any] = {"timeout": timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        hits: List[ImageHit] = []
        seen: set[str] = set()

        def add(raw: Any, *, source_url: str = "", source_title: str = "") -> None:
            if isinstance(raw, str):
                url = raw.strip()
                description = ""
                thumbnail = ""
            elif isinstance(raw, dict):
                url = str(raw.get("url") or raw.get("image_url") or "").strip()
                description = str(raw.get("description") or raw.get("title") or "").strip()
                thumbnail = str(
                    raw.get("thumbnail_url") or raw.get("thumbnailUrl") or ""
                ).strip()
            else:
                return
            if not url.startswith("https://") or url in seen:
                return
            seen.add(url)
            hits.append(
                ImageHit(
                    image_url=url,
                    title=description,
                    source_url=source_url,
                    source_title=source_title,
                    provider="tavily",
                    thumbnail_url=thumbnail,
                )
            )

        # Build a URL -> source-page map first so top-level query images can
        # still carry attribution when the same image appears in a result.
        source_map: Dict[str, tuple[str, str]] = {}
        for result in data.get("results") or []:
            if not isinstance(result, dict):
                continue
            source_url = str(result.get("url") or "").strip()
            source_title = str(result.get("title") or "").strip()
            for image in result.get("images") or []:
                url = (
                    str(image.get("url") or "").strip()
                    if isinstance(image, dict)
                    else str(image or "").strip()
                )
                if url:
                    source_map.setdefault(url, (source_url, source_title))

        # Query-level images are generally more relevant than all images
        # scraped from one long travel page; put them first.
        for image in data.get("images") or []:
            url = (
                str(image.get("url") or "").strip()
                if isinstance(image, dict)
                else str(image or "").strip()
            )
            source_url, source_title = source_map.get(url, ("", ""))
            add(image, source_url=source_url, source_title=source_title)

        # Then diversify per-result images instead of taking every image from
        # the first result page (which often includes logos/hotels/maps).
        for result in data.get("results") or []:
            if not isinstance(result, dict):
                continue
            source_url = str(result.get("url") or "").strip()
            source_title = str(result.get("title") or "").strip()
            for image in (result.get("images") or [])[:2]:
                add(image, source_url=source_url, source_title=source_title)
        return hits[: max(1, max_results)]


class ImageSearchTool(Tool):
    # `image_search` is reserved by DeepSeek when server-side web tools are
    # enabled. The local function tool must use a non-reserved name so it can
    # coexist with deepseek_web_search/deepseek_read_url.
    name = "web_image_search"
    description = (
        "联网搜索图片并下载到本地媒体条。适合找照片、配图、参考图、地点/人物图片；"
        "返回来源页面和许可信息，图片会直接显示在回答下方。"
        "当用户要求“找图/搜图/配一张图”时优先使用，不要声称没有图片搜索能力。"
    )
    default_risk: RiskLevel = "safe"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "图片搜索关键词，尽量具体"},
            "en_query": {
                "type": "string",
                "description": "可选：简短英文关键词。中文搜图时建议同时提供，例如 query=呼伦贝尔草原, en_query=Hulunbuir grassland",
            },
            "count": {
                "type": "integer",
                "description": "返回并下载的图片数量（默认 4，最大 8）",
                "minimum": 1,
                "maximum": 8,
            },
        },
        "required": ["query"],
    }

    def __init__(
        self,
        config: ImageSearchToolConfig,
        *,
        provider: Optional[TavilyImageSearchProvider] = None,
        cache: Optional[MediaCache] = None,
    ) -> None:
        self.config = config
        self.provider = provider or TavilyImageSearchProvider(config)
        self.cache = cache or MediaCache(
            config.cache_dir,
            max_bytes=config.max_bytes,
            timeout_seconds=min(8.0, config.timeout_seconds),
        )

    def update_config(self, config: ImageSearchToolConfig) -> None:
        self.config = config
        self.provider = TavilyImageSearchProvider(config)
        self.cache = MediaCache(
            config.cache_dir,
            max_bytes=config.max_bytes,
            timeout_seconds=min(8.0, config.timeout_seconds),
        )

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        query = str(arguments.get("query") or "")[:28]
        return f"正在联网搜图：{query}…"

    async def execute(
        self,
        arguments: Dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolResult(
                ok=False, output="query 不能为空", summary="缺少图片搜索词", status="error"
            )
        count = min(
            8,
            max(1, int(arguments.get("count") or self.config.max_results or 4)),
        )
        candidate_limit = min(16, count * 2 + 2)
        effective_query = str(arguments.get("en_query") or "").strip() or query
        try:
            hits = await self.provider.search(
                effective_query,
                max_results=candidate_limit,
                safe_search=self.config.safe_search,
            )
        except Exception as exc:
            safe_err = ctx.config.redact_secrets(f"{type(exc).__name__}: {exc}")
            logger.warning("image_search failed: %s", safe_err)
            return ToolResult(
                ok=False,
                output=f"图片搜索失败: {safe_err}",
                summary="图片搜索失败",
                status="error",
            )
        if not hits:
            return ToolResult(
                ok=True,
                output="没有找到可用的图片结果。",
                summary="未找到图片",
                status="ok",
            )

        media_items: List[Dict[str, Any]] = []
        failures = 0
        semaphore = asyncio.Semaphore(4)

        async def fetch_one(hit: ImageHit) -> Optional[Dict[str, Any]]:
            nonlocal failures
            async with semaphore:
                urls = [hit.image_url]
                if hit.thumbnail_url and hit.thumbnail_url != hit.image_url:
                    urls.append(hit.thumbnail_url)
                for url in urls:
                    try:
                        media = await self.cache.download(
                            url,
                            title=hit.title or hit.source_title or query,
                            source_url=hit.source_url,
                            source_title=hit.source_title,
                            license=hit.license,
                            attribution=hit.attribution,
                            provider=hit.provider,
                        )
                        return media.as_media_item()
                    except MediaDownloadError as exc:
                        logger.info("Image candidate failed %s: %s", url, exc)
                    except Exception as exc:
                        logger.warning("Unexpected image download error: %s", type(exc).__name__)
                failures += 1
                return None

        tasks = [asyncio.create_task(fetch_one(hit)) for hit in hits[:candidate_limit]]
        try:
            for task in asyncio.as_completed(tasks):
                item = await task
                if item is not None:
                    media_items.append(item)
                    if len(media_items) >= count:
                        break
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

        if not media_items:
            return ToolResult(
                ok=False,
                output="找到了图片结果，但图片下载均失败（可能源站防盗链或链接已失效）。",
                summary="图片下载失败",
                status="error",
            )

        try:
            if ctx.memory_backend is not None:
                refs = await ctx.memory_backend.list_referenced_media_paths()
                self.cache.prune(refs)
        except Exception:
            logger.debug("Media cache prune skipped", exc_info=True)

        lines = [f"已找到并下载 {len(media_items)} 张图片，已放入媒体条："]
        for index, item in enumerate(media_items, start=1):
            source = item.get("source_title") or item.get("source_url") or ""
            suffix = f"（来源：{source}）" if source else ""
            lines.append(f"{index}. {item.get('title') or '图片'}{suffix}")
        if failures:
            lines.append(f"另有 {failures} 张图片下载失败，未展示。")
        return ToolResult(
            ok=True,
            output="\n".join(lines),
            summary=f"已下载 {len(media_items)} 张图片",
            status="ok",
            metadata={
                "media_paths": [str(item["local_path"]) for item in media_items],
                "media_items": media_items,
                "count": len(media_items),
            },
        )
