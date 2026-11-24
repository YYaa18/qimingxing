from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from brain.config import WebSearchToolConfig
from brain.tools.base import Tool, ToolExecutionContext, ToolResult, truncate_head_tail
from protocol.models import RiskLevel

logger = logging.getLogger("qimingxing.tools.web_search")


@dataclass
class SearchHit:
    title: str
    url: str
    summary: str
    date: str = ""
    site_name: str = ""


@dataclass
class FetchPageResult:
    url: str
    title: str
    content: str


class SearchProvider(ABC):
    """Pluggable web search and page-fetch provider interface."""

    @abstractmethod
    async def search(self, query: str, *, max_results: int = 5) -> List[SearchHit]:
        """Search the web for `query`."""

    @abstractmethod
    async def fetch(self, url: str) -> FetchPageResult:
        """Fetch clean Markdown/text content for `url`."""


class DeepSeekSearchProvider(SearchProvider):
    """Official DeepSeek Search & Fetch API provider (`/v1/tools/search` and `/v1/tools/fetch`)."""

    def __init__(
        self,
        config: WebSearchToolConfig,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.config = config
        self._transport = transport

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    async def search(self, query: str, *, max_results: int = 5) -> List[SearchHit]:
        if not self.config.api_key:
            raise RuntimeError("WebSearchTool 缺少 DeepSeek API Key。")
        url = f"{self.config.base_url}/tools/search"
        timeout = httpx.Timeout(self.config.timeout_seconds, connect=8.0)
        async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
            resp = await client.post(url, headers=self._headers(), json={"query": query})
            resp.raise_for_status()
            data = resp.json()
        raw_results = data.get("results") or []
        hits: List[SearchHit] = []
        for item in raw_results[: max(1, max_results)]:
            hits.append(
                SearchHit(
                    title=str(item.get("title") or "无标题"),
                    url=str(item.get("url") or ""),
                    summary=str(item.get("summary") or item.get("snippet") or "").strip(),
                    date=str(item.get("date") or ""),
                    site_name=str(item.get("site_name") or ""),
                )
            )
        return hits

    async def fetch(self, url: str) -> FetchPageResult:
        if not self.config.api_key:
            raise RuntimeError("WebFetchTool 缺少 DeepSeek API Key。")
        endpoint = f"{self.config.base_url}/tools/fetch"
        timeout = httpx.Timeout(self.config.timeout_seconds, connect=8.0)
        async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
            resp = await client.post(endpoint, headers=self._headers(), json={"url": url})
            resp.raise_for_status()
            data = resp.json()
        return FetchPageResult(
            url=url,
            title=str(data.get("title") or url),
            content=str(data.get("content") or ""),
        )


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "联网搜索实时信息、技术文档、版本发布说明或未知报错解决方案。"
        "当问题涉及最新动态或需要核实外部资料时优先使用。"
    )
    default_risk: RiskLevel = "safe"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "精准搜索关键词或问题，支持 site: 等语法",
            },
            "max_results": {
                "type": "integer",
                "description": "返回结果条数（默认 5，最大 10）",
                "minimum": 1,
                "maximum": 10,
            },
        },
        "required": ["query"],
    }

    def __init__(self, config: WebSearchToolConfig, provider: Optional[SearchProvider] = None) -> None:
        self.config = config
        self.provider = provider or DeepSeekSearchProvider(config)

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        q = str(arguments.get("query") or "")[:24]
        return f"正在联网搜索：{q}…"

    async def execute(
        self,
        arguments: Dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, output="query 不能为空", summary="缺少搜索词", status="error")
        max_results = min(
            10,
            max(1, int(arguments.get("max_results") or self.config.max_results)),
        )
        try:
            hits = await self.provider.search(query, max_results=max_results)
        except Exception as exc:
            safe_err = ctx.config.redact_secrets(f"{type(exc).__name__}: {exc}")
            logger.warning("web_search failed: %s", safe_err)
            return ToolResult(
                ok=False,
                output=f"联网搜索失败: {safe_err}",
                summary="联网搜索失败",
                status="error",
            )

        if not hits:
            return ToolResult(ok=True, output="未检索到相关网页结果。", summary="无搜索结果")

        lines = [f"以下为「{query}」的实时联网搜索结果（共 {len(hits)} 条）："]
        for i, hit in enumerate(hits, start=1):
            meta_parts = [p for p in (hit.site_name, hit.date[:10] if hit.date else "") if p]
            meta_str = f" ({' | '.join(meta_parts)})" if meta_parts else ""
            lines.append(
                f"{i}. **{hit.title}**{meta_str}\n"
                f"   URL: {hit.url}\n"
                f"   摘要: {truncate_head_tail(hit.summary, 420)}"
            )
        rendered = ctx.config.redact_secrets("\n".join(lines))
        return ToolResult(
            ok=True,
            output=truncate_head_tail(rendered, self.config.max_fetch_chars),
            summary=f"联网搜索命中 {len(hits)} 条结果",
            metadata={"count": len(hits)},
        )


class WebFetchTool(Tool):
    name = "web_fetch"
    description = "读取并提取指定网页 URL 的 Markdown 正文内容，适合深入阅读 web_search 返回的链接。"
    default_risk: RiskLevel = "safe"
    parameters_schema = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "以 http:// 或 https:// 开头的网页地址",
            }
        },
        "required": ["url"],
    }

    def __init__(self, config: WebSearchToolConfig, provider: Optional[SearchProvider] = None) -> None:
        self.config = config
        self.provider = provider or DeepSeekSearchProvider(config)

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return "正在读取网页正文…"

    async def execute(
        self,
        arguments: Dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        url = str(arguments.get("url") or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return ToolResult(
                ok=False,
                output="url 必须以 http:// 或 https:// 开头",
                summary="URL 格式无效",
                status="error",
            )
        try:
            page = await self.provider.fetch(url)
        except Exception as exc:
            safe_err = ctx.config.redact_secrets(f"{type(exc).__name__}: {exc}")
            logger.warning("web_fetch failed: %s", safe_err)
            return ToolResult(
                ok=False,
                output=f"读取网页失败: {safe_err}",
                summary="读取网页失败",
                status="error",
            )

        body = ctx.config.redact_secrets(page.content or "")
        trimmed = truncate_head_tail(body, self.config.max_fetch_chars)
        return ToolResult(
            ok=True,
            output=f"# {page.title}\nURL: {page.url}\n\n{trimmed}",
            summary=f"已读取网页「{page.title[:24]}」",
        )
