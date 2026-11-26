from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from brain.context_builder import estimate_tokens
from brain.llm_router import LLMRouter, LLMUsage
from brain.tools.base import ToolExecutionContext, ToolResult, ToolTraceStep
from brain.tools.registry import ToolRegistry
from brain.utils import truncate_head_tail

logger = logging.getLogger("qimingxing.tools.executor")


@dataclass
class ToolLoopResult:
    final_text: str
    trace_steps: List[ToolTraceStep] = field(default_factory=list)
    usages: List[LLMUsage] = field(default_factory=list)
    stopped_reason: str = "completed"  # "completed" | "max_steps" | "circuit_breaker"
    called_tools: List[str] = field(default_factory=list)
    sources: List[Dict[str, Any]] = field(default_factory=list)


class ToolExecutor:
    """Multi-step native Function Calling orchestrator with risk guards and turn cancellation."""

    def __init__(self, llm_router: LLMRouter) -> None:
        self.llm_router = llm_router

    async def run(
        self,
        *,
        messages: List[Dict[str, Any]],
        registry: ToolRegistry,
        ctx: ToolExecutionContext,
        profile_name: Optional[str] = None,
        on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None,
        max_steps: Optional[int] = None,
        max_context_tokens: Optional[int] = None,
        tools_schema: Optional[List[Dict[str, Any]]] = None,
        on_server_tool_event: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    ) -> ToolLoopResult:
        limit = max(1, max_steps if max_steps is not None else ctx.config.tools.max_tool_steps)
        schema = tools_schema if tools_schema is not None else registry.to_openai_tools()
        working_messages: List[Dict[str, Any]] = [dict(m) for m in messages]

        spoken_parts: List[str] = []
        trace_steps: List[ToolTraceStep] = []
        usages: List[LLMUsage] = []
        called_tools: List[str] = []
        final_sources: List[Dict[str, Any]] = []
        consecutive_errors: Dict[str, int] = {}

        async def _emit_delta(tok: str) -> None:
            spoken_parts.append(tok)
            if on_text_delta is not None:
                await on_text_delta(tok)

        stopped_reason = "completed"
        circuit_broken = False

        for step_idx in range(1, limit + 1):
            if ctx.is_cancelled():
                raise asyncio.CancelledError("Turn cancelled before LLM tool step")

            if max_context_tokens is not None:
                working_messages = self._fold_old_tool_outputs(
                    working_messages,
                    max_tokens=max_context_tokens,
                    keep_last=2,
                )

            completion_kwargs: Dict[str, Any] = {
                "profile_name": profile_name,
                "on_text_delta": _emit_delta,
            }
            if on_server_tool_event is not None and self._supports_server_events():
                completion_kwargs["on_server_tool_event"] = on_server_tool_event
            res = await self.llm_router.complete_with_tools(
                working_messages,
                schema,
                **completion_kwargs,
            )
            usages.append(res.usage)
            ctx.usage_collector.append(res.usage)

            if not res.tool_calls:
                return ToolLoopResult(
                    final_text="".join(spoken_parts),
                    trace_steps=trace_steps,
                    usages=usages,
                    stopped_reason="completed",
                    called_tools=called_tools,
                    sources=list(res.annotations),
                )

            assistant_message: Dict[str, Any] = {
                "role": "assistant",
                "content": res.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": tc.arguments_json or "{}",
                        },
                        **(
                            {"thought_signature": tc.thought_signature}
                            if tc.thought_signature
                            else {}
                        ),
                    }
                    for tc in res.tool_calls
                ],
            }
            # DeepSeek requires the full reasoning_content of every tool-using
            # round to be passed back; omitting it returns HTTP 400.
            if res.reasoning_content:
                assistant_message["reasoning_content"] = res.reasoning_content
            if res.server_tool_calls:
                assistant_message["server_tool_calls"] = res.server_tool_calls
            working_messages.append(assistant_message)

            for tc in res.tool_calls:
                if ctx.is_cancelled():
                    raise asyncio.CancelledError("Turn cancelled during tool dispatch")

                called_tools.append(tc.name)
                args = tc.parsed_arguments()
                tool = registry.get(tc.name)
                if tool is None:
                    err_msg = f"Error: tool '{tc.name}' is not registered."
                    trace_steps.append(
                        ToolTraceStep(
                            step=step_idx,
                            tool_name=tc.name,
                            arguments=args,
                            risk_level="blocked",
                            status="error",
                            summary="未注册工具",
                            output_preview=err_msg,
                        )
                    )
                    consecutive_errors[tc.name] = consecutive_errors.get(tc.name, 0) + 1
                    working_messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": err_msg}
                    )
                    if consecutive_errors[tc.name] >= 2:
                        circuit_broken = True
                        stopped_reason = "circuit_breaker"
                    continue

                risk = tool.classify_risk(args)
                if risk == "blocked":
                    logger.warning("Blocked dangerous tool call: tool=%s turn=%s", tc.name, ctx.turn_id)
                    await ctx.emit_status(
                        "error",
                        tc.name,
                        f"已拦截高危操作：{tc.name}",
                        step_idx,
                        limit,
                    )
                    blocked_out = (
                        "BLOCKED: 该命令或操作命中系统安全红线，已被硬拦截拒绝执行。"
                        "请向用户说明安全风险，切勿尝试绕过安全限制。"
                    )
                    trace_steps.append(
                        ToolTraceStep(
                            step=step_idx,
                            tool_name=tc.name,
                            arguments=args,
                            risk_level="blocked",
                            status="blocked",
                            summary="命中安全红线已拦截",
                            output_preview=blocked_out,
                        )
                    )
                    consecutive_errors[tc.name] = consecutive_errors.get(tc.name, 0) + 1
                    working_messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": blocked_out}
                    )
                    if consecutive_errors[tc.name] >= 2:
                        circuit_broken = True
                        stopped_reason = "circuit_breaker"
                    continue

                scope_key = tool.approval_scope_key(args)
                if risk == "confirm" and not (
                    scope_key is not None and ctx.is_session_approved(scope_key)
                ):
                    summary = tool.format_approval_summary(args)
                    details = tool.format_approval_details(args)
                    await ctx.emit_status(
                        "running",
                        tc.name,
                        f"等待确认：{summary}",
                        step_idx,
                        limit,
                    )
                    approved, remember = await ctx.request_approval(
                        tool_name=tc.name,
                        risk_level=risk,
                        summary=summary,
                        details=details,
                    )
                    if ctx.is_cancelled():
                        raise asyncio.CancelledError("Turn cancelled during approval wait")
                    if approved and remember and scope_key is not None:
                        ctx.mark_session_approved(scope_key)
                    if not approved:
                        await ctx.emit_status(
                            "error",
                            tc.name,
                            f"已拒绝/超时：{tc.name}",
                            step_idx,
                            limit,
                        )
                        denied_out = (
                            "DENIED: 用户拒绝或审批超时，未执行该操作。"
                            "请尊重用户决定并直接汇报情况，不要重复请求同一操作。"
                        )
                        trace_steps.append(
                            ToolTraceStep(
                                step=step_idx,
                                tool_name=tc.name,
                                arguments=args,
                                risk_level=risk,
                                status="denied",
                                summary="用户拒绝或超时",
                                output_preview=denied_out,
                            )
                        )
                        working_messages.append(
                            {"role": "tool", "tool_call_id": tc.id, "content": denied_out}
                        )
                        continue

                await ctx.emit_status(
                    "running",
                    tc.name,
                    tool.format_status_label(args),
                    step_idx,
                    limit,
                )
                t0 = time.perf_counter()
                try:
                    result = await tool.execute(args, ctx)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Tool execution failed unexpectedly: tool=%s", tc.name)
                    safe_err = ctx.config.redact_secrets(f"{type(exc).__name__}: {exc}")
                    result = ToolResult(
                        ok=False,
                        output=f"工具执行发生异常: {safe_err}",
                        summary=f"异常: {type(exc).__name__}",
                        status="error",
                    )
                elapsed_ms = int((time.perf_counter() - t0) * 1000)

                if result.ok:
                    consecutive_errors[tc.name] = 0
                else:
                    consecutive_errors[tc.name] = consecutive_errors.get(tc.name, 0) + 1

                await ctx.emit_status(
                    "completed" if result.ok else "error",
                    tc.name,
                    result.summary or f"{tc.name} 完成",
                    step_idx,
                    limit,
                )
                trace_steps.append(
                    ToolTraceStep(
                        step=step_idx,
                        tool_name=tc.name,
                        arguments=args,
                        risk_level=risk,
                        status=result.status if not result.ok else "ok",
                        summary=result.summary,
                        output_preview=result.output[:300],
                        elapsed_ms=elapsed_ms,
                    )
                )
                tool_output = truncate_head_tail(
                    result.output,
                    max_chars=6000,
                    notice="工具输出已按单次 8K token 预算截断",
                )
                working_messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": tool_output}
                )
                if consecutive_errors.get(tc.name, 0) >= 2:
                    logger.warning(
                        "Circuit breaker tripped for tool=%s after 2 consecutive errors",
                        tc.name,
                    )
                    circuit_broken = True
                    stopped_reason = "circuit_breaker"
                    break

            if circuit_broken:
                break
            if step_idx == limit:
                stopped_reason = "max_steps"

        # Produce a final natural-language wrap-up without tools when cap/circuit breaker stops the loop
        if not ctx.is_cancelled():
            guard_note = (
                "同一工具连续报错两次，已自动止损停止重试。请向用户如实说明当前结果与遇到的问题，不要继续调用工具。"
                if stopped_reason == "circuit_breaker"
                else f"已达到单轮工具最大步数上限（{limit} 步）。请基于目前已经拿到的信息向用户汇总当前进展与结果。"
            )
            wrap_messages = working_messages + [{"role": "system", "content": guard_note}]
            if max_context_tokens is not None:
                wrap_messages = self._fold_old_tool_outputs(
                    wrap_messages,
                    max_tokens=max_context_tokens,
                    keep_last=2,
                )
                wrap_messages = self._truncate_tool_outputs_to_fit(
                    wrap_messages,
                    max_tokens=max_context_tokens,
                )
            wrap_kwargs: Dict[str, Any] = {
                "profile_name": profile_name,
                "on_text_delta": _emit_delta,
            }
            if on_server_tool_event is not None and self._supports_server_events():
                wrap_kwargs["on_server_tool_event"] = on_server_tool_event
            final_res = await self.llm_router.complete_with_tools(
                wrap_messages,
                [],
                **wrap_kwargs,
            )
            usages.append(final_res.usage)
            ctx.usage_collector.append(final_res.usage)
            final_sources = list(final_res.annotations)

        return ToolLoopResult(
            final_text="".join(spoken_parts),
            trace_steps=trace_steps,
            usages=usages,
            stopped_reason=stopped_reason,
            called_tools=called_tools,
            sources=final_sources,
        )

    def _supports_server_events(self) -> bool:
        try:
            params = inspect.signature(self.llm_router.complete_with_tools).parameters
        except (TypeError, ValueError):
            return False
        return "on_server_tool_event" in params

    @staticmethod
    def _message_tokens(messages: List[Dict[str, Any]]) -> int:
        return sum(estimate_tokens(str(m.get("content", ""))) + 4 for m in messages)

    def _truncate_tool_outputs_to_fit(
        self,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: int,
    ) -> List[Dict[str, Any]]:
        """Hard-fit the wrap-up request by head/tail truncating residual tool outputs."""
        if max_tokens <= 0:
            return messages
        total = self._message_tokens(messages)
        while total > max_tokens:
            candidates = [
                (len(str(m.get("content", ""))), idx)
                for idx, m in enumerate(messages)
                if m.get("role") == "tool" and len(str(m.get("content", ""))) > 240
            ]
            if not candidates:
                logger.warning(
                    "wrap-up still over budget after tool folding: %d > %d tokens",
                    total,
                    max_tokens,
                )
                break
            length, idx = max(candidates)
            content = str(messages[idx].get("content", ""))
            cut = max(200, int(length * 0.55))
            messages[idx] = {
                **messages[idx],
                "content": truncate_head_tail(
                    content,
                    max_chars=cut,
                    notice="工具输出为适配收尾请求已头尾截断",
                ),
            }
            total = self._message_tokens(messages)
        return messages

    @staticmethod
    def _brief_tool_args(arguments_json: str) -> str:
        raw = (arguments_json or "").strip()
        if not raw:
            return ""
        try:
            args = json.loads(raw)
        except Exception:
            return raw[:80]
        if not isinstance(args, dict):
            return raw[:80]
        safe: Dict[str, Any] = {}
        for key, value in args.items():
            lower = str(key).lower()
            text = str(value)
            if any(tok in lower for tok in ("key", "token", "secret", "password", "authorization")):
                safe[key] = "***"
            elif "sk-" in text.lower() or "bearer " in text.lower():
                safe[key] = "***"
            else:
                safe[key] = value
        try:
            rendered = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            rendered = raw
        return rendered[:80]

    def _tool_call_lookup(self, messages: List[Dict[str, Any]]) -> Dict[str, tuple[str, str]]:
        lookup: Dict[str, tuple[str, str]] = {}
        for msg in messages:
            for call in msg.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                cid = str(call.get("id") or "")
                fn = call.get("function") or {}
                if cid and isinstance(fn, dict):
                    lookup[cid] = (
                        str(fn.get("name") or "tool"),
                        str(fn.get("arguments") or ""),
                    )
        return lookup

    def _fold_old_tool_outputs(
        self,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: int,
        keep_last: int = 2,
    ) -> List[Dict[str, Any]]:
        """Replace older long tool observations with a one-line marker.

        Keeps the latest few tool outputs intact so the model can continue the
        current step without an extra summarization LLM call.
        """
        if max_tokens <= 0:
            return messages
        total = sum(estimate_tokens(str(m.get("content", ""))) + 4 for m in messages)
        if total <= max_tokens:
            return messages

        tool_indices = [
            index for index, message in enumerate(messages)
            if message.get("role") == "tool"
        ]
        call_lookup = self._tool_call_lookup(messages)
        foldable = (
            tool_indices[:-keep_last]
            if keep_last > 0 and len(tool_indices) > keep_last
            else ([] if keep_last > 0 else tool_indices)
        )
        for order, index in enumerate(foldable, start=1):
            message = messages[index]
            content = str(message.get("content", ""))
            if len(content) <= 400:
                continue
            call_id = str(message.get("tool_call_id") or "")
            tool_name, args_json = call_lookup.get(call_id, ("tool", ""))
            brief = self._brief_tool_args(args_json)
            call_text = f"{tool_name}({brief})" if brief else tool_name
            message["content"] = (
                f"[第 {order} 步 {call_text} 输出已折叠，共 {len(content)} 字符；"
                "如需原文可查 work_history 或按相同参数重调]"
            )
            total = sum(estimate_tokens(str(m.get("content", ""))) + 4 for m in messages)
            if total <= max_tokens:
                break
        return messages

