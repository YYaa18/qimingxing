from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from brain.config import ContextBudgetConfig
from brain.context_store import ContextStore
from brain.memory.models import MemoryCategory, MemoryFact, MemorySkill, RetrievedMemory
from brain.prompts import BASE_SYSTEM_PROMPT

_ACTIVITY_INTENT_RE = re.compile(
    r"(刚才|刚刚|这会儿|最近|这段时间|十几分钟|之前).{0,10}(忙什么|干嘛|做什么|看什么|活动|时间线|足迹|干啥)"
    r"|(时间线|桌面足迹|活动记录|刚在忙)"
)

SOURCE_ATTRIBUTION_REMINDER = (
    "【结论来源标注要求】引用以下挂载文件、本地路径或工具返回的实测内容时，"
    "请简短标明“根据本地文件/工具结果”；属于推测或建议的部分请明确标出“以下为推测/建议”，"
    "若缺乏依据请直白说明，切勿编造或瞎猜。"
)


def get_grounding_guidelines() -> str:
    """Return canonical source attribution & honest-boundary guidelines injected into prompts."""
    return f"{BASE_SYSTEM_PROMPT}\n{SOURCE_ATTRIBUTION_REMINDER}"


def estimate_tokens(text: str) -> int:
    """Lightweight CJK/ASCII token estimator without heavy tokenizer dependencies."""
    if not text:
        return 0
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    other_chars = max(0, len(text) - cjk_chars)
    # ~1.3 tokens per CJK char, ~0.35 tokens per ASCII/punctuation char
    return max(1, int(math.ceil(cjk_chars * 1.3 + other_chars * 0.35)))


def estimate_message_tokens(msg: Dict[str, Any]) -> int:
    content = msg.get("content", "")
    if isinstance(content, list):
        total = 4
        for part in content:
            if part.get("type") == "text":
                total += estimate_tokens(str(part.get("text", "")))
            elif part.get("type") == "image_url":
                total += 256  # fixed estimate for a compressed 1280px JPEG
        return total
    return estimate_tokens(str(content)) + 4


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    if max_tokens <= 0 or not text:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    # Binary/proportional trim preserving readability
    ratio = max_tokens / max(1, estimate_tokens(text))
    cut = max(16, int(len(text) * ratio * 0.92))
    return text[:cut].rstrip() + "…"


def truncate_ocr_smart(text: str, max_tokens: int) -> str:
    """Strip blank lines and preserve head (60%) + tail (40%) when OCR text exceeds budget."""
    if not text or max_tokens <= 0:
        return ""
    cleaned_lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    cleaned = "\n".join(cleaned_lines)
    if estimate_tokens(cleaned) <= max_tokens:
        return cleaned

    head_budget = int(max_tokens * 0.58)
    tail_budget = max_tokens - head_budget - 12
    ratio = max_tokens / max(1, estimate_tokens(cleaned))
    total_chars = max(40, int(len(cleaned) * ratio * 0.88))
    head_chars = max(20, int(total_chars * 0.6))
    tail_chars = max(20, total_chars - head_chars)
    if tail_budget <= 0:
        return truncate_to_tokens(cleaned, max_tokens)

    head = cleaned[:head_chars].rstrip()
    tail = cleaned[-tail_chars:].lstrip()
    return f"{head}\n...（中间冗余内容已按预算省略）...\n{tail}"


def needs_full_timeline(user_text: str) -> bool:
    if not user_text:
        return False
    return bool(_ACTIVITY_INTENT_RE.search(user_text))


class TokenMeter:
    """Heuristic token counter with a clamped EMA correction from API usage."""

    def __init__(self) -> None:
        self.scale = 1.0

    def count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, int(round(estimate_tokens(text) * self.scale)))

    def count_message(self, msg: Dict[str, Any]) -> int:
        return max(1, int(round(estimate_message_tokens(msg) * self.scale)))

    def observe(self, estimated: int, actual: int) -> None:
        if estimated <= 0 or actual <= 0:
            return
        ratio = actual / estimated
        self.scale = min(1.8, max(0.6, 0.7 * self.scale + 0.3 * ratio))


@dataclass
class BuildStats:
    l0_tokens: int = 0
    l1_tokens: int = 0
    l2_summary_tokens: int = 0
    l2_recent_tokens: int = 0
    l3_tokens: int = 0
    l3_section_tokens: int = 0
    l3_skills_tokens: int = 0
    l3_attachments_tokens: int = 0
    total_tokens: int = 0
    total_with_tools: int = 0
    tools_tokens: int = 0
    resolved_total_input: int = 0
    recent_budget: int = 0
    calibration_scale: float = 1.0
    static_prefix_hash: str = ""
    recalled_count: int = 0
    recalled_skills_count: int = 0
    retrieval_ms: float = 0.0
    timeline_expanded: bool = False
    ocr_truncated: bool = False
    has_image_input: bool = False


@dataclass
class BuiltContext:
    messages: List[Dict[str, Any]]
    stats: BuildStats = field(default_factory=BuildStats)



def sanitize_recent_messages(
    messages: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """Keep provider-safe alternating history.

    A failed turn can leave a user message in SQLite without an assistant
    reply; DeepSeek then rejects the next request with "an assistant message
    must follow a user message". Coalesce same-role neighbours and drop a
    trailing unanswered user message; the live user turn is appended by
    ``build`` itself.
    """
    cleaned: List[Dict[str, str]] = []
    for msg in messages or []:
        role = str(msg.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        text = content if isinstance(content, str) else str(content or "")
        text = text.strip()
        if not text:
            continue
        if cleaned and cleaned[-1]["role"] == role:
            cleaned[-1]["content"] = cleaned[-1]["content"] + "\n\n" + text
        else:
            cleaned.append({"role": role, "content": text})
    while cleaned and cleaned[0]["role"] == "assistant":
        cleaned.pop(0)
    while cleaned and cleaned[-1]["role"] == "user":
        cleaned.pop()
    return cleaned

class ContextBuilder:
    """4-layer Prefix-Cache-friendly prompt assembler with strict per-slot Token budgets."""

    def __init__(self, budget: Optional[ContextBudgetConfig] = None) -> None:
        self.budget = budget or ContextBudgetConfig()
        self.meter = TokenMeter()

    def observe_prompt_usage(
        self,
        estimated_prompt_tokens: int,
        actual_prompt_tokens: int,
        *,
        has_images: bool = False,
    ) -> None:
        if has_images:
            # Image token accounting differs wildly between providers and
            # resolutions. Never let a multimodal turn shift the pure-text EMA.
            return
        self.meter.observe(estimated_prompt_tokens, actual_prompt_tokens)

    def build(
        self,
        *,
        user_text: str,
        user_content: Union[str, List[Dict[str, Any]]],
        core_facts: List[MemoryFact],
        rolling_summary: str,
        recent_messages: List[Dict[str, str]],
        recalled_memories: List[RetrievedMemory],
        context_store: ContextStore,
        categories: Optional[Dict[str, MemoryCategory]] = None,
        recalled_skills: Optional[List[MemorySkill]] = None,
        extra_l3_sections: Optional[List[str]] = None,
        retrieval_ms: float = 0.0,
        system_base_prompt: str = BASE_SYSTEM_PROMPT,
        resolved_budget: Optional[Any] = None,
        tools_tokens: int = 0,
    ) -> BuiltContext:
        recent_messages = sanitize_recent_messages(recent_messages)
        stats = BuildStats(
            retrieval_ms=retrieval_ms,
            recalled_count=len(recalled_memories),
            recalled_skills_count=len(recalled_skills or []),
            tools_tokens=tools_tokens,
            calibration_scale=self.meter.scale,
            has_image_input=bool(
                isinstance(user_content, list)
                and any(
                    isinstance(part, dict) and part.get("type") == "image_url"
                    for part in user_content
                )
            ),
        )

        # --- L0 (100% static) + L1 (low-frequency deterministic user profile) ---
        l0_text = system_base_prompt.strip()
        stats.l0_tokens = self.meter.count(l0_text)

        l1_cap = (
            int(resolved_budget.l1_cap)
            if resolved_budget is not None
            else self.budget.l1_profile_tokens
        )
        l1_block = self._render_l1_profile(core_facts, categories=categories, max_tokens=l1_cap)
        stats.l1_tokens = self.meter.count(l1_block)

        static_system_parts = [l0_text]
        if l1_block:
            static_system_parts.append(l1_block)
        static_system = "\n\n".join(static_system_parts)
        stats.static_prefix_hash = hashlib.sha256(static_system.encode("utf-8")).hexdigest()[:16]

        messages: List[Dict[str, Any]] = [{"role": "system", "content": static_system}]

        # --- L2: Rolling summary + Recent raw turns (within token budget) ---
        summary_cap = (
            int(resolved_budget.summary_cap)
            if resolved_budget is not None
            else self.budget.l2_summary_tokens
        )
        if rolling_summary and rolling_summary.strip() and summary_cap > 0:
            trimmed_summary = truncate_to_tokens(rolling_summary.strip(), summary_cap)
            summary_msg = {
                "role": "system",
                "content": f"【本会话早期对话滚动摘要】\n{trimmed_summary}",
            }
            stats.l2_summary_tokens = self.meter.count_message(summary_msg)
            messages.append(summary_msg)

        # --- L3: High-dynamic tail (recalled memories + denoised desktop context + OCR/vision) ---
        if resolved_budget is not None:
            recall_cap = int(resolved_budget.recall_cap)
            skills_cap = int(resolved_budget.skills_cap)
            attachments_cap = int(resolved_budget.attachments_cap)
            desktop_cap = int(resolved_budget.desktop_cap)
            l3_total_cap = int(resolved_budget.l3_total_cap)
        else:
            recall_cap = self.budget.l3_recall_tokens
            skills_cap = self.budget.l3_skills_tokens
            attachments_cap = self.budget.l3_attachments_tokens
            desktop_cap = self.budget.l3_desktop_ocr_tokens
            l3_total_cap = 0  # legacy mode: no separate L3 total cap

        l3_blocks: List[tuple[str, int, str]] = []  # (kind, priority, text)

        recall_block = self._render_l3_recalled(recalled_memories, max_tokens=recall_cap)
        if recall_block:
            l3_blocks.append(("recall", 0, recall_block))

        skills_block = self._render_l3_skills(recalled_skills or [], max_tokens=skills_cap)
        if skills_block:
            l3_blocks.append(("skills", 3, skills_block))

        extra_texts = [s.strip() for s in (extra_l3_sections or []) if s and s.strip()]
        if extra_texts:
            extra_text = "\n\n".join(extra_texts)
            extra_text = truncate_to_tokens(extra_text, attachments_cap)
            if extra_text:
                extra_text += "\n\n" + SOURCE_ATTRIBUTION_REMINDER
                l3_blocks.append(("attachments", 2, extra_text))

        expand_tl = needs_full_timeline(user_text)
        stats.timeline_expanded = expand_tl
        desktop_raw = (
            context_store.render_full_timeline()
            if expand_tl
            else context_store.render_compact_focus()
        )
        desktop_trimmed = truncate_to_tokens(desktop_raw, desktop_cap)
        if desktop_trimmed:
            l3_blocks.append(("desktop", 1, f"【当前桌面感官上下文】\n{desktop_trimmed}"))

        if l3_total_cap > 0:
            l3_blocks = self._fit_l3_blocks(l3_blocks, l3_total_cap)

        l3_sections = [text for _, _, text in l3_blocks]
        stats.l3_section_tokens = sum(self.meter.count(text) for text in l3_sections)
        stats.l3_skills_tokens = sum(
            self.meter.count(text) for kind, _, text in l3_blocks if kind == "skills"
        )
        stats.l3_attachments_tokens = sum(
            self.meter.count(text) for kind, _, text in l3_blocks if kind == "attachments"
        )

        final_user_content = self._apply_ocr_budget(
            user_content,
            stats,
            max_tokens=desktop_cap,
        )
        if l3_sections:
            tail_context = "\n\n".join(l3_sections)
            if isinstance(final_user_content, list):
                new_parts: List[Dict[str, Any]] = []
                injected = False
                for part in final_user_content:
                    if part.get("type") == "text" and not injected:
                        new_parts.append(
                            {
                                "type": "text",
                                "text": f"{tail_context}\n\n【本轮用户提问】\n{part.get('text', '')}",
                            }
                        )
                        injected = True
                    else:
                        new_parts.append(part)
                if not injected:
                    new_parts.insert(0, {"type": "text", "text": tail_context})
                final_user_content = new_parts
            else:
                final_user_content = (
                    f"{tail_context}\n\n【本轮用户提问】\n{final_user_content}"
                )

        user_msg = {"role": "user", "content": final_user_content}
        user_index = len(messages)
        messages.append(user_msg)
        stats.l3_tokens = stats.l3_section_tokens + self.meter.count_message(
            {"role": "user", "content": final_user_content}
        )

        # --- Recent raw history: fill the remainder after L1/summary/L3 ---
        if resolved_budget is not None:
            available = max(
                512,
                int(resolved_budget.total_input)
                - stats.l0_tokens
                - int(resolved_budget.tools_tokens),
            )
            used_other = stats.l1_tokens + stats.l2_summary_tokens + stats.l3_tokens
            recent_budget = max(0, available - used_other)
            stats.resolved_total_input = int(resolved_budget.total_input)
        else:
            recent_budget = self.budget.l2_recent_tokens
        stats.recent_budget = recent_budget

        kept_recent: List[Dict[str, str]] = []
        recent_used = 0
        for msg in reversed(recent_messages):
            cost = self.meter.count_message(msg)
            if kept_recent and recent_used + cost > recent_budget:
                break
            kept_recent.append({"role": msg["role"], "content": msg["content"]})
            recent_used += cost
        kept_recent.reverse()
        # Insert recent messages after system/summary, before the current user.
        messages[user_index:user_index] = kept_recent
        stats.l2_recent_tokens = recent_used

        stats.total_tokens = sum(self.meter.count_message(m) for m in messages)
        stats.total_with_tools = stats.total_tokens + int(tools_tokens)

        # Final hard safety cap: drop oldest recent messages first.
        hard_cap = (
            int(resolved_budget.total_input)
            if resolved_budget is not None
            else self.budget.total_max_tokens
        )
        while stats.total_with_tools > hard_cap and kept_recent:
            dropped = kept_recent.pop(0)
            messages.remove(dropped)
            stats.l2_recent_tokens -= self.meter.count_message(dropped)
            stats.total_tokens = sum(self.meter.count_message(m) for m in messages)
            stats.total_with_tools = stats.total_tokens + int(tools_tokens)

        stats.calibration_scale = self.meter.scale
        return BuiltContext(messages=messages, stats=stats)

    def _fit_l3_blocks(
        self,
        blocks: List[tuple[str, int, str]],
        cap: int,
    ) -> List[tuple[str, int, str]]:
        """Drop low-priority L3 sections until under cap; truncate the last one."""
        fitted = list(blocks)
        while fitted and sum(self.meter.count(text) for _, _, text in fitted) > cap:
            drop_idx = max(range(len(fitted)), key=lambda i: fitted[i][1])
            kind, priority, text = fitted[drop_idx]
            if priority <= 0:
                # Only the highest-priority section remains; truncate it.
                truncated = truncate_to_tokens(text, cap)
                if truncated:
                    fitted[drop_idx] = (kind, priority, truncated)
                else:
                    fitted.pop(drop_idx)
                break
            fitted.pop(drop_idx)
        return fitted

    def _render_l1_profile(
        self,
        core_facts: List[MemoryFact],
        *,
        categories: Optional[Dict[str, MemoryCategory]] = None,
        max_tokens: int,
    ) -> str:
        if not core_facts or max_tokens <= 0:
            return ""
        eligible = []
        for fact in core_facts:
            if fact.status != "active":
                continue
            if categories is not None and fact.category in categories:
                if not categories[fact.category].inject_to_l1:
                    continue
            elif fact.category == "chitchat":
                continue
            eligible.append(fact)

        ranked = sorted(
            eligible,
            key=lambda f: (-f.importance, -f.updated_at, f.fact_id),
        )
        selected: List[MemoryFact] = []
        used = 0
        for fact in ranked:
            line = f"- [{fact.category}] {fact.content}"
            cost = self.meter.count(line)
            if selected and used + cost > max_tokens:
                continue
            if not selected and cost > max_tokens:
                break
            selected.append(fact)
            used += cost

        if not selected:
            return ""
        selected.sort(key=lambda f: f.fact_id)
        lines = "\n".join(f"- [{f.category}] {f.content}" for f in selected)
        return f"【核心用户画像（稳定长期事实）】\n{lines}"

    def _fit_single_l3_line(self, line: str, max_tokens: int) -> str:
        if max_tokens <= 0:
            return ""
        fitted = truncate_ocr_smart(line, max_tokens)
        guard = 0
        while self.meter.count(fitted) > max_tokens and len(fitted) > 24 and guard < 6:
            guard += 1
            fitted = truncate_to_tokens(fitted, max(16, int(max_tokens * 0.75)))
        return fitted

    def _render_l3_recalled(
        self,
        recalled: List[RetrievedMemory],
        *,
        max_tokens: int,
    ) -> str:
        if not recalled or max_tokens <= 0:
            return ""
        lines: List[str] = []
        used = 0
        for item in recalled:
            if item.item_type == "fact":
                tag = "事实"
            elif item.item_type == "message":
                tag = "历史对话"
            else:
                tag = "情景"
            line = f"- ({tag}/{item.category}) {item.content}"
            cost = self.meter.count(line)
            if used + cost > max_tokens:
                if not lines:
                    line = self._fit_single_l3_line(line, max_tokens)
                    lines.append(line)
                    used = min(max_tokens, self.meter.count(line))
                break
            lines.append(line)
            used += cost
        if not lines:
            return ""
        return "【相关长期记忆召回】\n" + "\n".join(lines)

    def _render_l3_skills(
        self,
        skills: List[MemorySkill],
        *,
        max_tokens: int = 1000,
    ) -> str:
        if not skills or max_tokens <= 0:
            return ""
        import json as _json

        lines: List[str] = []
        used = 0
        for s in skills:
            try:
                steps = _json.loads(s.steps_json)
                step_text = " -> ".join(str(x) for x in steps[:5]) if isinstance(steps, list) else str(steps)
            except Exception:
                step_text = s.steps_json
            pre = f"（前置: {s.prerequisites}）" if s.prerequisites else ""
            line = (
                f"- [skill:{s.skill_id} | 工具={s.tool_preference} | 成功{s.success_count}次] "
                f"{s.name}{pre}: {step_text}"
            )
            cost = self.meter.count(line)
            if used + cost > max_tokens:
                if not lines:
                    line = self._fit_single_l3_line(line, max_tokens)
                    lines.append(line)
                    used = min(max_tokens, self.meter.count(line))
                break
            lines.append(line)
            used += cost
        if not lines:
            return ""
        return "【可复用的已学技能（Skill，按需参考执行）】\n" + "\n".join(lines)

    def _apply_ocr_budget(
        self,
        user_content: Union[str, List[Dict[str, Any]]],
        stats: BuildStats,
        *,
        max_tokens: Optional[int] = None,
    ) -> Union[str, List[Dict[str, Any]]]:
        if isinstance(user_content, list):
            return user_content
        text = str(user_content)
        budget = max_tokens if max_tokens is not None else self.budget.l3_desktop_ocr_tokens
        if budget <= 0:
            return text
        if estimate_tokens(text) <= budget:
            return text
        stats.ocr_truncated = True
        return truncate_ocr_smart(text, budget)
