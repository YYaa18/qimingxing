from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from brain.config import AppConfig, MCPServerConfig
from brain.tools.base import Tool, ToolExecutionContext, ToolResult
from brain.tools.mcp_oauth import MCPOAuthCoordinator
from brain.utils import truncate_head_tail
from protocol.models import RiskLevel

logger = logging.getLogger("qimingxing.tools.mcp")

MCP_MAX_OUTPUT_CHARS = 4000

_TRANSPORT_DISCONNECT_RE = re.compile(
    r"(?:closedresourceerror|brokenresourceerror|endofstream|connecterror|"
    r"remoteprotocolerror|connection\s+closed|broken\s+pipe|connection\s+reset|"
    r"connection\s+aborted|connection\s+refused|stream\s+closed|session\s+closed|"
    r"server\s+closed|not\s+connected|process\s+exited|\beof\b|transport|disconnect)",
    re.IGNORECASE,
)


def _is_transport_disconnect_error(exc: BaseException) -> bool:
    """Return True if `exc` indicates a dropped/crashed MCP transport rather than timeout or logic errors."""
    visited: set[int] = set()
    queue: List[BaseException] = [exc]
    while queue:
        cur = queue.pop(0)
        if id(cur) in visited:
            continue
        visited.add(id(cur))
        if isinstance(cur, (asyncio.CancelledError, TimeoutError, asyncio.TimeoutError)):
            continue
        if isinstance(cur, (ConnectionError, EOFError)):
            return True
        sig = f"{type(cur).__name__}: {cur}"
        if _TRANSPORT_DISCONNECT_RE.search(sig):
            return True
        if cur.__cause__ is not None:
            queue.append(cur.__cause__)
        if cur.__context__ is not None:
            queue.append(cur.__context__)
        sub_excs = getattr(cur, "exceptions", None)
        if isinstance(sub_excs, (list, tuple)):
            queue.extend(sub_excs)
    return False


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
        session: Any = None,
        manager: Optional["MCPClientManager"] = None,
        is_safe: bool,
        timeout_sec: float = 30.0,
    ) -> None:
        self.server_name = server_name
        self.raw_tool_name = raw_tool_name
        self.name = f"mcp__{server_name}__{raw_tool_name}"
        self.description = f"[MCP:{server_name}] {description or raw_tool_name}"
        self.parameters_schema = parameters_schema or {"type": "object", "properties": {}}
        self._session = session
        self._manager = manager
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
            if self._manager is not None:
                raw_res = await self._manager.call_tool(
                    self.server_name,
                    self.raw_tool_name,
                    arguments,
                    timeout_sec=self._timeout_sec,
                )
            elif self._session is not None:
                raw_res = await asyncio.wait_for(
                    self._session.call_tool(self.raw_tool_name, arguments=arguments),
                    timeout=max(1.0, self._timeout_sec),
                )
            else:
                raise ConnectionError(f"MCP server '{self.server_name}' 会话未连接")
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
            safe_err = truncate_head_tail(
                safe_err,
                MCP_MAX_OUTPUT_CHARS,
                notice="MCP 异常信息已按 4,000 字符预算截断",
            )
            return ToolResult(
                ok=False,
                output=f"MCP 工具调用失败: {safe_err}",
                summary="MCP 调用异常",
                status="error",
            )

        is_err = bool(getattr(raw_res, "isError", False))
        text = _extract_mcp_result_text(raw_res) or ("MCP 工具执行失败" if is_err else "（执行成功，无文本输出）")
        text = ctx.config.redact_secrets(text)
        trimmed = truncate_head_tail(
            text,
            MCP_MAX_OUTPUT_CHARS,
            notice="MCP 返回内容已按 4,000 字符预算截断",
        )
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
    status: str  # "disabled" | "connected" | "offline" | "needs_auth"
    tools: List[str] = field(default_factory=list)
    error: str = ""
    auth_mode: str = "none"
    url: str = ""
    has_oauth_token: bool = False


def _format_exc_summary(exc: BaseException) -> str:
    sub_excs = getattr(exc, "exceptions", None)
    if sub_excs:
        parts = [_format_exc_summary(sub) for sub in list(sub_excs)[:3]]
        return "; ".join(p for p in parts if p)
    msg = str(exc).strip()
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


@dataclass
class _ServerRuntime:
    config: MCPServerConfig
    stack: Optional[AsyncExitStack] = None
    session: Any = None
    tool_names: List[str] = field(default_factory=list)
    generation: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


TransportOpener = Callable[[MCPServerConfig, AsyncExitStack], Any]


class MCPClientManager:
    """Lifecycle manager for stdio / SSE / Streamable HTTP MCP servers with strict fault isolation."""

    def __init__(
        self,
        config: AppConfig,
        *,
        transport_opener: Optional[TransportOpener] = None,
        oauth_coordinator: Optional[MCPOAuthCoordinator] = None,
    ) -> None:
        self.config = config
        self._transport_opener = transport_opener
        self.oauth_coordinator = oauth_coordinator
        self._registry: Any = None
        self._reload_lock = asyncio.Lock()
        self._runtimes: Dict[str, _ServerRuntime] = {}
        self._statuses: Dict[str, MCPServerStatus] = {}
        self._auth_tasks: Dict[str, asyncio.Task[bool]] = {}
        self._sync_server_tables()

    def _has_oauth_token(self, srv: MCPServerConfig) -> bool:
        if srv.auth_mode != "oauth" or self.oauth_coordinator is None:
            return False
        return self.oauth_coordinator.has_saved_token(srv.name)

    def _sync_server_tables(self) -> None:
        new_runtimes: Dict[str, _ServerRuntime] = {}
        new_statuses: Dict[str, MCPServerStatus] = {}
        for srv in self.config.mcp_servers:
            prev_rt = self._runtimes.get(srv.name)
            new_runtimes[srv.name] = _ServerRuntime(
                config=srv,
                stack=prev_rt.stack if prev_rt is not None else None,
                session=prev_rt.session if prev_rt is not None else None,
                tool_names=list(prev_rt.tool_names) if prev_rt is not None else [],
                generation=prev_rt.generation if prev_rt is not None else 0,
                lock=prev_rt.lock if prev_rt is not None else asyncio.Lock(),
            )
            has_tok = self._has_oauth_token(srv)
            init_status = "disabled" if not srv.enabled else (
                "needs_auth" if (srv.auth_mode == "oauth" and self.oauth_coordinator is not None and not has_tok) else "offline"
            )
            new_statuses[srv.name] = MCPServerStatus(
                name=srv.name,
                transport=srv.transport,
                enabled=srv.enabled,
                status=init_status,
                auth_mode=srv.auth_mode,
                url=srv.url,
                has_oauth_token=has_tok,
            )
        self._runtimes = new_runtimes
        self._statuses = new_statuses

    async def start_all(self, registry: Any) -> List[MCPServerStatus]:
        """Connect all enabled MCP servers cleanly; failed servers stay isolated and offline."""
        async with self._reload_lock:
            return await self._start_all_locked(registry)

    async def _start_all_locked(self, registry: Any) -> List[MCPServerStatus]:
        self._registry = registry
        self._sync_server_tables()

        async def _connect(srv: MCPServerConfig) -> None:
            runtime = self._runtimes[srv.name]
            async with runtime.lock:
                await self._connect_server_locked(runtime, registry)

        enabled = [srv for srv in self.config.mcp_servers if srv.enabled]
        if enabled:
            # Startup time was the sum of every server's handshake; run them
            # concurrently (each server already has its own lock/state).
            await asyncio.gather(*(_connect(srv) for srv in enabled))
        return self.list_statuses()

    async def _connect_server_locked(
        self,
        runtime: _ServerRuntime,
        registry: Any,
        *,
        interactive_auth: bool = False,
    ) -> bool:
        srv = runtime.config
        has_tok = self._has_oauth_token(srv)
        st = self._statuses.setdefault(
            srv.name,
            MCPServerStatus(
                name=srv.name,
                transport=srv.transport,
                enabled=srv.enabled,
                status="disabled" if not srv.enabled else "offline",
                auth_mode=srv.auth_mode,
                url=srv.url,
                has_oauth_token=has_tok,
            ),
        )
        st.auth_mode = srv.auth_mode
        st.url = srv.url
        st.has_oauth_token = has_tok
        if not srv.enabled:
            st.status = "disabled"
            st.tools = []
            st.error = ""
            return False

        if (
            srv.auth_mode == "oauth"
            and not interactive_auth
            and self.oauth_coordinator is not None
            and not has_tok
        ):
            st.status = "needs_auth"
            st.tools = []
            st.error = "需要点击「OAuth 授权」完成浏览器登录"
            logger.info("MCP server '%s' awaiting interactive OAuth authorization", srv.name)
            return False

        stack = AsyncExitStack()
        connect_timeout = (
            max(185.0, float(srv.startup_timeout_sec))
            if interactive_auth
            else max(2.0, float(srv.startup_timeout_sec))
        )
        try:
            session, tools_registered = await asyncio.wait_for(
                self._connect_one(srv, stack, registry, interactive_auth=interactive_auth),
                timeout=connect_timeout,
            )
            runtime.stack = stack
            runtime.session = session
            runtime.tool_names = list(tools_registered)
            runtime.generation += 1
            st.status = "connected"
            st.tools = list(tools_registered)
            st.error = ""
            st.has_oauth_token = self._has_oauth_token(srv)
            logger.info(
                "Connected MCP server '%s' (%s): registered %d tool(s): %s",
                srv.name,
                srv.transport,
                len(tools_registered),
                ", ".join(tools_registered),
            )
            return True
        except BaseException as exc:
            safe_err = self.config.redact_secrets(_format_exc_summary(exc))
            runtime.stack = None
            runtime.session = None
            runtime.tool_names = []
            is_auth_err = (
                srv.auth_mode == "oauth"
                and (
                    "oauth" in safe_err.lower()
                    or "401" in safe_err
                    or "unauthorized" in safe_err.lower()
                    or "invalid_token" in safe_err.lower()
                    or "未授权" in safe_err
                )
            )
            st.status = "needs_auth" if is_auth_err else "offline"
            st.tools = []
            st.error = safe_err[:240]
            st.has_oauth_token = self._has_oauth_token(srv)
            if isinstance(exc, asyncio.CancelledError):
                logger.debug("MCP server '%s' startup cancelled", srv.name)
            else:
                logger.warning(
                    "MCP server '%s' failed to start (isolated cleanly): %s",
                    srv.name,
                    st.error,
                )
            try:
                await stack.aclose()
            except BaseException:
                pass
            if isinstance(exc, asyncio.CancelledError):
                raise
            return False

    async def _open_session(
        self,
        srv: MCPServerConfig,
        stack: AsyncExitStack,
        *,
        interactive_auth: bool = False,
    ) -> Any:
        if self._transport_opener is not None:
            return await self._transport_opener(srv, stack)

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.sse import sse_client
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client

        oauth_provider = None
        if srv.auth_mode == "oauth" and self.oauth_coordinator is not None and srv.url:
            oauth_provider = self.oauth_coordinator.build_provider(
                srv.name,
                srv.url,
                interactive=interactive_auth,
            )

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
                sse_client(srv.url, headers=srv.headers or None, auth=oauth_provider)
            )
        elif srv.transport == "streamable_http":
            if not srv.url:
                raise ValueError(f"MCP streamable_http server '{srv.name}' 缺少 url")
            http_client = None
            if srv.headers or oauth_provider is not None:
                import httpx2

                http_client = await stack.enter_async_context(
                    httpx2.AsyncClient(
                        headers=srv.headers or None,
                        auth=oauth_provider,
                        follow_redirects=True,
                    )
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
        *,
        interactive_auth: bool = False,
    ) -> Tuple[Any, List[str]]:
        session = await self._open_session(srv, stack, interactive_auth=interactive_auth)
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
                manager=self,
                is_safe=(raw_name in safe_set),
                timeout_sec=srv.timeout_sec,
            )
            registry.register(adapter)
            registered.append(adapter.name)

        return session, sorted(registered)

    async def _close_server_runtime_locked(
        self,
        runtime: _ServerRuntime,
        registry: Any = None,
        *,
        unregister_tools: bool = True,
    ) -> None:
        reg = registry if registry is not None else self._registry
        if unregister_tools and reg is not None:
            for name in runtime.tool_names:
                reg.unregister(name)
        runtime.tool_names.clear()
        runtime.session = None
        stack = runtime.stack
        runtime.stack = None
        if stack is not None:
            try:
                await asyncio.wait_for(stack.aclose(), timeout=5.0)
            except Exception as exc:
                logger.debug("Ignoring MCP shutdown error for '%s': %s", runtime.config.name, exc)
        st = self._statuses.get(runtime.config.name)
        if st is not None and st.status == "connected":
            st.status = "offline"
            st.tools = []

    async def _close_server_runtime(
        self,
        server_name: str,
        registry: Any = None,
        *,
        unregister_tools: bool = True,
    ) -> None:
        runtime = self._runtimes.get(server_name)
        if runtime is None:
            return
        async with runtime.lock:
            await self._close_server_runtime_locked(
                runtime, registry, unregister_tools=unregister_tools
            )

    async def reconnect_server(
        self,
        server_name: str,
        registry: Any = None,
        *,
        expected_generation: Optional[int] = None,
    ) -> bool:
        """Close the stale runtime for `server_name` and attempt a single clean reconnect."""
        runtime = self._runtimes.get(server_name)
        if runtime is None or not runtime.config.enabled:
            return False
        reg = registry if registry is not None else self._registry
        async with runtime.lock:
            if (
                expected_generation is not None
                and runtime.generation != expected_generation
                and runtime.session is not None
            ):
                return True
            await self._close_server_runtime_locked(runtime, reg, unregister_tools=True)
            return await self._connect_server_locked(runtime, reg)

    async def call_tool(
        self,
        server_name: str,
        raw_tool_name: str,
        arguments: Dict[str, Any],
        *,
        timeout_sec: float = 30.0,
    ) -> Any:
        """Invoke an MCP tool with one automatic reconnect-and-retry on transport disconnect."""
        runtime = self._runtimes.get(server_name)
        if runtime is None:
            raise ConnectionError(f"未找到 MCP server '{server_name}'")

        session = runtime.session
        gen = runtime.generation
        if session is None:
            reconnected = await self.reconnect_server(server_name, expected_generation=gen)
            if not reconnected or runtime.session is None:
                st = self._statuses.get(server_name)
                detail = st.error if st and st.error else "无法建立连接"
                raise ConnectionError(f"MCP server '{server_name}' 未连接: {detail}")
            session = runtime.session
            gen = runtime.generation

        timeout = max(1.0, float(timeout_sec))
        try:
            return await asyncio.wait_for(
                session.call_tool(raw_tool_name, arguments=arguments),
                timeout=timeout,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise
        except Exception as exc:
            if not _is_transport_disconnect_error(exc):
                raise

            logger.warning(
                "MCP tool %s.%s transport disconnected (%s: %s); attempting single auto-reconnect...",
                server_name,
                raw_tool_name,
                type(exc).__name__,
                self.config.redact_secrets(str(exc))[:160],
            )
            reconnected = await self.reconnect_server(server_name, expected_generation=gen)
            if not reconnected or runtime.session is None:
                st = self._statuses.get(server_name)
                if st is not None:
                    st.status = "offline"
                raise ConnectionError(
                    f"MCP transport disconnected ({type(exc).__name__}: {exc}); auto-reconnect failed"
                ) from exc

            new_session = runtime.session
            try:
                return await asyncio.wait_for(
                    new_session.call_tool(raw_tool_name, arguments=arguments),
                    timeout=timeout,
                )
            except Exception as retry_exc:
                if _is_transport_disconnect_error(retry_exc):
                    st = self._statuses.get(server_name)
                    if st is not None:
                        st.status = "offline"
                        st.error = self.config.redact_secrets(
                            f"{type(retry_exc).__name__}: {retry_exc}"
                        )[:240]
                raise

    async def reload(
        self,
        registry: Any,
        servers: Optional[List[MCPServerConfig]] = None,
    ) -> List[MCPServerStatus]:
        """Hot-reload MCP connections and refresh ToolRegistry entries."""
        async with self._reload_lock:
            await self.stop_all(registry)
            if servers is not None:
                self.config.mcp_servers = list(servers)
            return await self._start_all_locked(registry)

    async def start_oauth_authorization(
        self,
        server_name: str,
        registry: Any = None,
        *,
        discovery_timeout_sec: float = 12.0,
    ) -> Dict[str, Any]:
        """Start interactive OAuth 2.1 DCR + PKCE authorization for a configured remote MCP server."""
        if self.oauth_coordinator is None:
            return {"status": "error", "error": "OAuth coordinator 未初始化"}
        runtime = self._runtimes.get(server_name)
        if runtime is None:
            return {"status": "error", "error": f"未找到 MCP server '{server_name}'"}
        srv = runtime.config
        if srv.transport not in ("streamable_http", "sse") or not srv.url:
            return {"status": "error", "error": "仅远程 URL MCP（streamable_http / sse）支持 OAuth 授权"}

        reg = registry if registry is not None else self._registry
        prev_task = self._auth_tasks.pop(server_name, None)
        if prev_task is not None and not prev_task.done():
            prev_task.cancel()

        auth_ev = self.oauth_coordinator.prepare_auth_session(server_name)

        async def _interactive_runner() -> bool:
            async with runtime.lock:
                await self._close_server_runtime_locked(runtime, reg, unregister_tools=True)
                return await self._connect_server_locked(runtime, reg, interactive_auth=True)

        auth_task = asyncio.create_task(_interactive_runner())
        self._auth_tasks[server_name] = auth_task
        ev_wait = asyncio.create_task(auth_ev.wait())
        try:
            await asyncio.wait(
                {auth_task, ev_wait},
                timeout=max(2.0, discovery_timeout_sec),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not ev_wait.done():
                ev_wait.cancel()

        st = self._statuses.get(server_name)
        if auth_task.done():
            self._auth_tasks.pop(server_name, None)
            try:
                ok = bool(auth_task.result())
            except Exception as exc:
                safe_err = self.config.redact_secrets(_format_exc_summary(exc))
                if st is not None:
                    st.error = safe_err[:240]
                return {
                    "status": "error",
                    "error": safe_err,
                    "server": asdict(st) if st else None,
                }
            if ok:
                return {
                    "status": "connected",
                    "auth_url": "",
                    "server": asdict(st) if st else None,
                }
            return {
                "status": "error",
                "error": (st.error if st else "") or "OAuth 连接失败",
                "server": asdict(st) if st else None,
            }

        auth_url = self.oauth_coordinator.pending_auth_url.get(server_name, "")
        if auth_url:
            return {
                "status": "authorizing",
                "auth_url": auth_url,
                "server": asdict(st) if st else None,
            }

        auth_task.cancel()
        self._auth_tasks.pop(server_name, None)
        return {
            "status": "error",
            "error": "获取远程 OAuth 授权地址超时 (>12s)",
            "server": asdict(st) if st else None,
        }

    async def stop_all(self, registry: Any = None) -> None:
        for task in list(self._auth_tasks.values()):
            if not task.done():
                task.cancel()
        self._auth_tasks.clear()
        reg = registry if registry is not None else self._registry
        for runtime in reversed(list(self._runtimes.values())):
            async with runtime.lock:
                await self._close_server_runtime_locked(
                    runtime, reg, unregister_tools=True
                )

    def list_statuses(self) -> List[MCPServerStatus]:
        return list(self._statuses.values())
