from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from brain.config import AppConfig
from brain.llm_router import LLMRouter, LLMUsage
from brain.memory.backend import MemoryBackend
from protocol.models import RiskLevel


def truncate_head_tail(text: str, max_chars: int, *, notice: str = "中间输出已按预算截断") -> str:
    """Keep 60% head and 40% tail when tool output exceeds max_chars."""
    if not text or max_chars <= 0 or len(text) <= max_chars:
        return text
    head_len = max(40, int(max_chars * 0.6))
    tail_len = max(40, max_chars - head_len - 32)
    omitted = len(text) - head_len - tail_len
    return (
        f"{text[:head_len].rstrip()}\n"
        f"...[{notice}，省略 {omitted} 字符]...\n"
        f"{text[-tail_len:].lstrip()}"
    )


@dataclass
class ToolTraceStep:
    step: int
    tool_name: str
    arguments: Dict[str, Any]
    risk_level: RiskLevel
    status: str  # "ok" | "blocked" | "denied" | "error" | "cancelled"
    summary: str = ""
    output_preview: str = ""
    elapsed_ms: int = 0


@dataclass
class ToolResult:
    ok: bool
    output: str
    summary: str = ""
    status: str = "ok"  # "ok" | "blocked" | "denied" | "error" | "cancelled"
    metadata: Dict[str, Any] = field(default_factory=dict)


ApprovalRequester = Callable[
    [str, RiskLevel, str, Dict[str, Any], int], Awaitable[Tuple[bool, bool]]
]
StatusEmitter = Callable[
    [str, str, str, Optional[int], Optional[int]], Awaitable[None]
]


class ToolExecutionContext:
    """Isolated runtime context passed to each tool execution inside a turn."""

    def __init__(
        self,
        *,
        turn_id: str,
        session_id: str,
        config: AppConfig,
        llm_router: Optional[LLMRouter] = None,
        memory_backend: Optional[MemoryBackend] = None,
        cancel_event: Optional[asyncio.Event] = None,
        session_approved_tools: Optional[Set[str]] = None,
        on_status: Optional[StatusEmitter] = None,
        on_approval: Optional[ApprovalRequester] = None,
        usage_collector: Optional[List[LLMUsage]] = None,
    ) -> None:
        self.turn_id = turn_id
        self.session_id = session_id
        self.config = config
        self.llm_router = llm_router
        self.memory_backend = memory_backend
        self.cancel_event = cancel_event or asyncio.Event()
        self.session_approved_tools: Set[str] = (
            session_approved_tools if session_approved_tools is not None else set()
        )
        self._on_status = on_status
        self._on_approval = on_approval
        self.usage_collector: List[LLMUsage] = (
            usage_collector if usage_collector is not None else []
        )

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def is_session_approved(self, tool_name: str) -> bool:
        return tool_name in self.session_approved_tools

    def mark_session_approved(self, tool_name: str) -> None:
        self.session_approved_tools.add(tool_name)

    async def emit_status(
        self,
        status: str,
        tool: str,
        label: str = "",
        step: Optional[int] = None,
        max_steps: Optional[int] = None,
    ) -> None:
        if self._on_status is not None:
            await self._on_status(status, tool, label, step, max_steps)

    async def request_approval(
        self,
        *,
        tool_name: str,
        risk_level: RiskLevel,
        summary: str,
        details: Optional[Dict[str, Any]] = None,
        timeout_sec: Optional[int] = None,
    ) -> Tuple[bool, bool]:
        if self._on_approval is None:
            return False, False
        wait_sec = (
            timeout_sec if timeout_sec is not None else self.config.tools.approval_timeout_sec
        )
        return await self._on_approval(
            tool_name, risk_level, summary, details or {}, wait_sec
        )


class Tool(ABC):
    """Abstract base class for Qimingxing native tools."""

    name: str
    description: str
    parameters_schema: Dict[str, Any]
    default_risk: RiskLevel = "confirm"

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        return self.default_risk

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return f"正在执行 {self.name}…"

    def format_approval_summary(self, arguments: Dict[str, Any]) -> str:
        return f"允许执行工具 `{self.name}`？"

    def format_approval_details(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return dict(arguments)

    def to_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }

    @abstractmethod
    async def execute(
        self,
        arguments: Dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        """Execute the tool and return a structured ToolResult."""
