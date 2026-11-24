from __future__ import annotations

from typing import Any, Dict, List, Optional

from brain.tools.base import Tool, ToolExecutionContext, ToolResult
from protocol.models import RiskLevel


class AskUserTool(Tool):
    """Lightweight SAFE tool allowing Brain to pause and ask the user for brief clarification."""

    name = "ask_user"
    description = (
        "当用户指令存在明显歧义或缺少关键选择（例如拖入多个文件却只说“整理一下”、"
        "或高影响操作存在多条可选路径）时，暂停并发起简短的澄清提问（建议附 1–3 个简短选项），"
        "待用户回复后再继续执行。禁止在信息已经明确时滥用。"
    )
    default_risk: RiskLevel = "safe"
    parameters_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "向用户提出的一句清晰简短的澄清问题。",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 4,
                "description": "可选：1–3 个简短选项按钮供用户快速点选。",
            },
        },
        "required": ["question"],
    }

    def approval_scope_key(self, arguments: Dict[str, Any]) -> Optional[str]:
        return None

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return "正在等待你的澄清确认…"

    async def execute(
        self,
        arguments: Dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        question = str(arguments.get("question") or "").strip()
        if not question:
            return ToolResult(
                ok=False,
                status="error",
                output="Error: `ask_user` requires a non-empty `question` string.",
                summary="澄清问题为空",
            )

        raw_opts = arguments.get("options") or []
        options: List[str] = []
        if isinstance(raw_opts, list):
            for opt in raw_opts:
                s = str(opt or "").strip()
                if s and s not in options:
                    options.append(s)
                if len(options) >= 4:
                    break

        await ctx.emit_status("running", self.name, f"等待确认：{question[:24]}", None, None)
        answer = await ctx.ask_user_clarification(question=question, options=options)
        if answer is None:
            return ToolResult(
                ok=False,
                status="denied",
                output=(
                    "用户未回复或取消了澄清确认。请勿盲目猜测或擅自调用写操作工具，"
                    "直接向用户简洁说明需要补充的信息。"
                ),
                summary="用户未确认",
            )

        clean_answer = answer.strip()
        return ToolResult(
            ok=True,
            output=f"用户已澄清回复：{clean_answer}",
            summary=f"用户澄清：{clean_answer[:40]}",
            metadata={"answer": clean_answer, "question": question},
        )
