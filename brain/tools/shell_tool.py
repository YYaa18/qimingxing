from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
from typing import Any, Dict, List, Optional

from brain.config import ShellToolConfig
from brain.tools.base import Tool, ToolExecutionContext, ToolResult, truncate_head_tail
from protocol.models import RiskLevel

logger = logging.getLogger("qimingxing.tools.shell")

_CHAIN_OR_REDIRECT_TOKENS = (";", "&&", "||", "|", ">", "<", "`", "$(", "\n", "\r")
_SENSITIVE_PATH_RE = re.compile(r"(\.ssh/|id_rsa|id_ed25519|config\.yaml|\.env\b)", re.IGNORECASE)


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


class ShellTool(Tool):
    name = "shell_exec"
    description = (
        "在本机受控工作目录执行命令行操作（支持只读查看、运行测试脚本或调用本机已安装的通用 CLI 工具）。"
        "高危破坏或读取密钥命令会被硬拦截；写入/执行/外部 CLI 会触发用户审批确认。"
    )
    default_risk: RiskLevel = "confirm"
    parameters_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的单条 Shell 命令字符串",
            },
            "cwd": {
                "type": "string",
                "description": "可选工作目录，默认使用系统配置根目录",
            },
        },
        "required": ["command"],
    }

    def __init__(self, config: ShellToolConfig) -> None:
        self.config = config
        self._blocked_res = [
            re.compile(pat, re.IGNORECASE) for pat in (config.blocked_patterns or []) if pat
        ]
        self._safe_prefixes = [s.strip() for s in (config.safe_commands or []) if s.strip()]

    def _resolve_cwd(self, raw_cwd: Optional[str]) -> str:
        chosen = (raw_cwd or self.config.default_cwd or "~").strip()
        expanded = os.path.abspath(os.path.expanduser(chosen))
        return expanded

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        cmd = str(arguments.get("command") or "").strip()
        if not cmd:
            return "blocked"
        if _SENSITIVE_PATH_RE.search(cmd):
            return "blocked"
        for pat in self._blocked_res:
            if pat.search(cmd):
                return "blocked"

        # SAFE only for simple single readonly commands with no shell chaining/redirection
        if any(tok in cmd for tok in _CHAIN_OR_REDIRECT_TOKENS):
            return "confirm"

        lower_cmd = cmd.lower()
        for prefix in self._safe_prefixes:
            lp = prefix.lower()
            if lower_cmd == lp or lower_cmd.startswith(lp + " "):
                return "safe"
        return "confirm"

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        cmd = str(arguments.get("command") or "").strip()[:36]
        return f"正在执行命令：{cmd}"

    def format_approval_summary(self, arguments: Dict[str, Any]) -> str:
        cmd = str(arguments.get("command") or "").strip()
        return f"允许执行本地命令：`{cmd[:80]}`？"

    def format_approval_details(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "command": str(arguments.get("command") or "").strip(),
            "cwd": self._resolve_cwd(arguments.get("cwd")),
            "timeout_sec": self.config.timeout_sec,
        }

    async def execute(
        self,
        arguments: Dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        cmd = str(arguments.get("command") or "").strip()
        if not cmd:
            return ToolResult(ok=False, output="command 不能为空", summary="缺少命令", status="error")

        risk = self.classify_risk(arguments)
        if risk == "blocked":
            return ToolResult(
                ok=False,
                output="BLOCKED: 命令命中安全红线，已被系统硬拦截。",
                summary="命中安全红线",
                status="blocked",
            )

        cwd = self._resolve_cwd(arguments.get("cwd"))
        if not os.path.isdir(cwd):
            return ToolResult(
                ok=False,
                output=f"工作目录不存在: {cwd}",
                summary="工作目录无效",
                status="error",
            )

        timeout = max(1, int(arguments.get("timeout_sec") or self.config.timeout_sec))
        proc: Optional[asyncio.subprocess.Process] = None
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                raw_out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                _kill_process_group(proc)
                try:
                    await proc.wait()
                except Exception:
                    pass
                return ToolResult(
                    ok=False,
                    output=f"命令执行超时（>{timeout}s），已强制终止子进程组并清理现场。",
                    summary=f"执行超时 (>{timeout}s)",
                    status="error",
                )
        except asyncio.CancelledError:
            if proc is not None:
                _kill_process_group(proc)
                try:
                    await proc.wait()
                except Exception:
                    pass
            raise
        except Exception as exc:
            safe_err = ctx.config.redact_secrets(f"{type(exc).__name__}: {exc}")
            return ToolResult(
                ok=False,
                output=f"启动命令失败: {safe_err}",
                summary="命令启动失败",
                status="error",
            )

        text = (raw_out or b"").decode("utf-8", errors="replace").strip()
        text = ctx.config.redact_secrets(text)
        trimmed = truncate_head_tail(text, self.config.max_output_chars)
        code = proc.returncode if proc.returncode is not None else -1
        ok = code == 0
        body = f"[exit_code={code} | cwd={cwd}]\n{trimmed if trimmed else '（无输出）'}"
        return ToolResult(
            ok=ok,
            output=body,
            summary=f"命令完成 (exit={code})" if ok else f"命令失败 (exit={code})",
            status="ok" if ok else "error",
            metadata={"exit_code": code, "cwd": cwd},
        )
