from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from brain.config import AppConfig, MCPServerConfig
from brain.tools.base import Tool, ToolExecutionContext, ToolResult
from brain.utils import truncate_head_tail
from protocol.models import RiskLevel

logger = logging.getLogger("qimingxing.tools.mcp")


def _extract_mcp_result_text(result: Any) -> str:
    content_blocks = getattr(result, "content", None) or []
    parts: List[str] = []
    for block in content_blocks:
        txt = getattr(block, "text", None)
        if txt is not None:
            parts.append(str(txt))
        elif hasattr(block, "model_dump"):
            parts.append(str(block.model_dump()))
        else:
            parts.append(str(block))
    structured = getattr(result, "structuredContent", None)
    if structured and not parts:
        parts.append(str(structured))
    return "\n".join(p for p in parts if p).strip()


class MCPToolAdapter(Tool):
    """Wraps one remote/local MCP tool cleanly into Qimingxing's ToolRegistry & 3-tier risk model."""

    def __init__(
        self,
        *,
        server_name: str,
        raw_tool_name: str,
        description: str,
        parameters_schema: Dict[str, Any],
        session: Any,
        is_safe: bool,
        timeout_sec: float = 30.0,
    ) -> None:
        self.server_name = server_name
        self.raw_tool_name = raw_tool_name
        self.name = f"mcp__{server_name}__{raw_tool_name}"
        self.description = f"[MCP:{server_name}] {description or raw_tool_name}"
        self.parameters_schema = parameters_schema or {"type": "object", "properties": {}}
        self._session = session
        self.default_risk: RiskLevel = "safe" if is_safe else "confirm"
        self._timeout_sec = timeout_sec

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        return self.default_risk

    def approval_scope_key(self, arguments: Dict[str, Any]) -> Optional[str]:
        # Follow P4-01 strictness: never session-cache non-whitelisted external MCP write/side-effect tools
        return self.name if self.default_risk == "safe" else None

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return f"正在调用 MCP 工具 {self.server_name}.{self.raw_tool_name}…"

    def format_approval_summary(self, arguments: Dict[str, Any]) -> str:
        return f"允许调用外部 MCP 工具 `{self.server_name}.{self.raw_tool_name}`？"

    def format_approval_details(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "server": self.server_name,
            "tool": self.raw_tool_name,
            "arguments": dict(arguments),
            "allow_remember": self.default_risk == "safe",
        }

    async def execute(
        self,
        arguments: Dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            raw_res = await asyncio.wait_for(
                self._session.call_tool(self.raw_tool_name, arguments=arguments),
                timeout=max(1.0, self._timeout_sec),
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return ToolResult(
                ok=False,
                output=f"MCP 工具 {self.server_name}.{self.raw_tool_name} 调用超时 (>{self._timeout_sec:.0f}s)",
                summary="MCP 调用超时",
                status="error",
            )
        except Exception as exc:
            safe_err = ctx.config.redact_secrets(f"{type(exc).__name__}: {exc}")
            return ToolResult(
                ok=False,
                output=f"MCP 工具调用失败: {safe_err}",
                summary="MCP 调用异常",
                status="error",
            )

        is_err = bool(getattr(raw_res, "isError", False))
        text = _extract_mcp_result_text(raw_res) or ("MCP 工具执行失败" if is_err else "（执行成功，无文本输出）")
        text = ctx.config.redact_secrets(text)
        trimmed = truncate_head_tail(text, 6000, notice="MCP 返回内容已按预算截断")
        return ToolResult(
            ok=not is_err,
            output=trimmed,
            summary=f"MCP {self.raw_tool_name} {'失败' if is_err else '完成'}",
            status="error" if is_err else "ok",
        )


@dataclass
class MCPServerStatus:
    name: str
    transport: str
    enabled: bool
    status: str  # "disabled" | "connected" | "offline"
    tools: List[str] = field(default_factory=list)
    error: str = ""


TransportOpener = Callable[[MCPServerConfig, AsyncExitStack], Any]


class MCPClientManager:
    """Lifecycle manager for stdio / SSE / Streamable HTTP MCP servers with strict fault isolation."""

    def __init__(
        self,
        config: AppConfig,
        *,
        transport_opener: Optional[TransportOpener] = None,
    ) -> None:
        self.config = config
        self._transport_opener = transport_opener
        self._stacks: List[AsyncExitStack] = []
        self._registered_tool_names: List[str] = []
        self._statuses: Dict[str, MCPServerStatus] = {
            srv.name: MCPServerStatus(
                name=srv.name,
                transport=srv.transport,
                enabled=srv.enabled,
                status="disabled" if not srv.enabled else "offline",
            )
            for srv in config.mcp_servers
        }

    async def start_all(self, registry: Any) -> List[MCPServerStatus]:
        """Connect all enabled MCP servers cleanly; failed servers stay isolated and offline."""
        for srv in self.config.mcp_servers:
            st = MCPServerStatus(
                name=srv.name,
                transport=srv.transport,
                enabled=srv.enabled,
                status="disabled" if not srv.enabled else "offline",
            )
            self._statuses[srv.name] = st
            if not srv.enabled:
                continue

            stack = AsyncExitStack()
            try:
                tools_registered = await asyncio.wait_for(
                    self._connect_one(srv, stack, registry),
                    timeout=max(2.0, float(srv.startup_timeout_sec)),
                )
                self._stacks.append(stack)
                st.status = "connected"
                st.tools = tools_registered
                logger.info(
                    "Connected MCP server '%s' (%s): registered %d tool(s): %s",
                    srv.name,
                    srv.transport,
                    len(tools_registered),
                    ", ".join(tools_registered),
                )
            except Exception as exc:
                safe_err = self.config.redact_secrets(f"{type(exc).__name__}: {exc}")
                st.status = "offline"
                st.error = safe_err[:240]
                logger.warning(
                    "MCP server '%s' failed to start (isolated cleanly): %s",
                    srv.name,
                    st.error,
                )
                try:
                    await stack.aclose()
                except Exception:
                    pass
        return self.list_statuses()

    async def _open_session(
        self, srv: MCPServerConfig, stack: AsyncExitStack
    ) -> Any:
        if self._transport_opener is not None:
            return await self._transport_opener(srv, stack)

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.sse import sse_client
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client

        if srv.transport == "stdio":
            if not srv.command:
                raise ValueError(f"MCP stdio server '{srv.name}' 缺少 command")
            merged_env = {**os.environ, **srv.env} if srv.env else None
            params = StdioServerParameters(
                command=srv.command,
                args=list(srv.args),
                env=merged_env,
            )
            read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
        elif srv.transport == "sse":
            if not srv.url:
                raise ValueError(f"MCP sse server '{srv.name}' 缺少 url")
            read_stream, write_stream = await stack.enter_async_context(
                sse_client(srv.url, headers=srv.headers or None)
            )
        elif srv.transport == "streamable_http":
            if not srv.url:
                raise ValueError(f"MCP streamable_http server '{srv.name}' 缺少 url")
            http_client = None
            if srv.headers:
                import httpx2

                http_client = await stack.enter_async_context(
                    httpx2.AsyncClient(headers=srv.headers)
                )
            streams = await stack.enter_async_context(
                streamable_http_client(srv.url, http_client=http_client)
            )
            read_stream, write_stream = streams[0], streams[1]
        else:
            raise ValueError(f"不支持的 MCP transport: {srv.transport}")

        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        return session

    async def _connect_one(
        self,
        srv: MCPServerConfig,
        stack: AsyncExitStack,
        registry: Any,
    ) -> List[str]:
        session = await self._open_session(srv, stack)
        listed = await session.list_tools()
        raw_tools = getattr(listed, "tools", listed) or []
        safe_set = set(srv.safe_tools or [])
        registered: List[str] = []

        for item in raw_tools:
            raw_name = str(getattr(item, "name", "") or "")
            if not raw_name:
                continue
            desc = str(getattr(item, "description", "") or "")
            schema = getattr(item, "inputSchema", None) or getattr(
                item, "input_schema", None
            ) or {"type": "object", "properties": {}}
            adapter = MCPToolAdapter(
                server_name=srv.name,
                raw_tool_name=raw_name,
                description=desc,
                parameters_schema=dict(schema) if isinstance(schema, dict) else {"type": "object", "properties": {}},
                session=session,
                is_safe=(raw_name in safe_set),
                timeout_sec=srv.timeout_sec,
            )
            registry.register(adapter)
            self._registered_tool_names.append(adapter.name)
            registered.append(adapter.name)

        return sorted(registered)

    async def stop_all(self, registry: Any = None) -> None:
        if registry is not None:
            for name in self._registered_tool_names:
                registry.unregister(name)
        self._registered_tool_names.clear()

        for stack in reversed(self._stacks):
            try:
                await asyncio.wait_for(stack.aclose(), timeout=5.0)
            except Exception as exc:
                logger.debug("Ignoring MCP shutdown error: %s", exc)
        self._stacks.clear()

        for st in self._statuses.values():
            if st.status == "connected":
                st.status = "offline"

    def list_statuses(self) -> List[MCPServerStatus]:
        return list(self._statuses.values())
