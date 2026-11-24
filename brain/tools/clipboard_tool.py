from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Awaitable, Callable, Dict, Optional

from brain.tools.base import Tool, ToolExecutionContext, ToolResult
from brain.utils import truncate_head_tail
from protocol.models import RiskLevel

logger = logging.getLogger("qimingxing.tools.clipboard")

ClipboardReaderFn = Callable[[], Awaitable[str]]


async def _default_read_clipboard() -> str:
    """Strictly on-demand OS clipboard read (zero background polling)."""
    if sys.platform != "darwin":
        raise RuntimeError(f"当前平台 {sys.platform} 暂未实现剪贴板读取器")
    proc = await asyncio.create_subprocess_exec(
        "pbpaste",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("读取系统剪贴板超时 (>2s)")
    if proc.returncode != 0:
        raise RuntimeError(f"pbpaste 失败: {err.decode('utf-8', errors='replace')[:120]}")
    return out.decode("utf-8", errors="replace")


class ClipboardTool(Tool):
    """On-demand clipboard reader protected by foreground PrivacyGuard checks."""

    name = "clipboard_read"
    description = (
        "按需读取用户当前系统剪贴板中的文本内容。"
        "仅在用户明确提到“看下我刚复制的内容/报错/剪贴板”时才可调用；禁止在无关对话中调用。"
    )
    default_risk: RiskLevel = "safe"
    parameters_schema = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, reader_fn: Optional[ClipboardReaderFn] = None) -> None:
        self._reader_fn = reader_fn or _default_read_clipboard

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return "正在按需读取剪贴板…"

    async def execute(
        self,
        arguments: Dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        # Enforce privacy blacklist before ever touching OS clipboard
        if ctx.context_store is not None and ctx.context_store.is_current_window_privacy_masked():
            logger.warning("Clipboard read blocked because active foreground window is privacy-masked")
            return ToolResult(
                ok=False,
                output=(
                    "PRIVACY_BLOCKED: 当前前台窗口命中隐私黑名单（如密码管理器或敏感窗口），"
                    "为防止误读敏感凭据已硬拦截剪贴板读取。"
                ),
                summary="隐私窗口已拦截剪贴板读取",
                status="privacy_blocked",
            )

        try:
            raw = await self._reader_fn()
        except Exception as exc:
            safe_err = ctx.config.redact_secrets(f"{type(exc).__name__}: {exc}")
            return ToolResult(
                ok=False,
                output=f"读取剪贴板失败: {safe_err}",
                summary="读取剪贴板失败",
                status="error",
            )

        cleaned = ctx.config.redact_secrets(raw.strip())
        if not cleaned:
            return ToolResult(ok=True, output="（当前剪贴板为空或不含纯文本内容）", summary="剪贴板为空")

        trimmed = truncate_head_tail(cleaned, 4000, notice="超长剪贴板文本已截断")
        return ToolResult(
            ok=True,
            output=f"【剪贴板内容（按需读取并已脱敏）】\n{trimmed}",
            summary=f"已读取剪贴板（{len(trimmed)} 字符）",
        )
