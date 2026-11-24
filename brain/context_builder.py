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


@dataclass
class BuildStats:
    l0_tokens: int = 0
    l1_tokens: int = 0
    l2_summary_tokens: int = 0
    l2_recent_tokens: int = 0
    l3_tokens: int = 0
    total_tokens: int = 0
    static_prefix_hash: str = ""
    recalled_count: int = 0
    recalled_skills_count: int = 0
    retrieval_ms: float = 0.0
    timeline_expanded: bool = False
    ocr_truncated: bool = False


@dataclass
class BuiltContext:
    messages: List[Dict[str, Any]]
    stats: BuildStats = field(default_factory=BuildStats)


class ContextBuilder:
    """4-layer Prefix-Cache-friendly prompt assembler with strict per-slot Token budgets."""

    def __init__(self, budget: Optional[ContextBudgetConfig] = None) -> None:
        self.budget = budget or ContextBudgetConfig()

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
    ) -> BuiltContext:
        stats = BuildStats(
            retrieval_ms=retrieval_ms,
            recalled_count=len(recalled_memories),
            recalled_skills_count=len(recalled_skills or []),
        )

        # --- L0 (100% static) + L1 (low-frequency deterministic user profile) ---
        l0_text = system_base_prompt.strip()
        stats.l0_tokens = estimate_tokens(l0_text)

        l1_block = self._render_l1_profile(core_facts, categories=categories)
        stats.l1_tokens = estimate_tokens(l1_block)

        static_system_parts = [l0_text]
        if l1_block:
            static_system_parts.append(l1_block)
        static_system = "\n\n".join(static_system_parts)
        stats.static_prefix_hash = hashlib.sha256(static_system.encode("utf-8")).hexdigest()[:16]

        messages: List[Dict[str, Any]] = [{"role": "system", "content": static_system}]

        # --- L2: Rolling summary + Recent raw turns (within token budget) ---
        if rolling_summary and rolling_summary.strip():
            trimmed_summary = truncate_to_tokens(
                rolling_summary.strip(), self.budget.l2_summary_tokens
            )
            summary_msg = {
                "role": "system",
                "content": f"【本会话早期对话滚动摘要】\n{trimmed_summary}",
            }
            stats.l2_summary_tokens = estimate_message_tokens(summary_msg)
            messages.append(summary_msg)

        kept_recent: List[Dict[str, str]] = []
        recent_used = 0
        for msg in reversed(recent_messages):
            cost = estimate_message_tokens(msg)
            if kept_recent and recent_used + cost > self.budget.l2_recent_tokens:
                break
            kept_recent.append({"role": msg["role"], "content": msg["content"]})
            recent_used += cost
        kept_recent.reverse()
        stats.l2_recent_tokens = recent_used
        messages.extend(kept_recent)

        # --- L3: High-dynamic tail (recalled memories + denoised desktop context + OCR/vision) ---
        l3_sections: List[str] = []

        recall_block = self._render_l3_recalled(recalled_memories)
        if recall_block:
            l3_sections.append(recall_block)

        skills_block = self._render_l3_skills(recalled_skills or [])
        if skills_block:
            l3_sections.append(skills_block)

        for extra_sec in extra_l3_sections or []:
            if extra_sec and extra_sec.strip():
                l3_sections.append(extra_sec.strip())
        if any(extra_sec and extra_sec.strip() for extra_sec in (extra_l3_sections or [])):
            l3_sections.append(SOURCE_ATTRIBUTION_REMINDER)

        expand_tl = needs_full_timeline(user_text)
        stats.timeline_expanded = expand_tl
        desktop_raw = (
            context_store.render_full_timeline()
            if expand_tl
            else context_store.render_compact_focus()
        )
        desktop_budget = self.budget.l3_desktop_ocr_tokens
        desktop_trimmed = truncate_to_tokens(desktop_raw, desktop_budget)
        if desktop_trimmed:
            l3_sections.append(f"【当前桌面感官上下文】\n{desktop_trimmed}")

        final_user_content = self._apply_ocr_budget(user_content, stats)
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
        messages.append(user_msg)
        stats.l3_tokens = estimate_tokens("\n\n".join(l3_sections)) + estimate_message_tokens(
            {"role": "user", "content": final_user_content}
        )
        stats.total_tokens = sum(estimate_message_tokens(m) for m in messages)

        # Keep exact L2 recent dict references so dropping oldest works reliably
        # Final hard safety cap against total_max_tokens by dropping oldest L2 recent messages if needed
        while stats.total_tokens > self.budget.total_max_tokens and kept_recent:
            dropped = kept_recent.pop(0)
            messages.remove(dropped)
            stats.l2_recent_tokens -= estimate_message_tokens(dropped)
            stats.total_tokens = sum(estimate_message_tokens(m) for m in messages)

        return BuiltContext(messages=messages, stats=stats)

    def _render_l1_profile(
        self,
        core_facts: List[MemoryFact],
        *,
        categories: Optional[Dict[str, MemoryCategory]] = None,
    ) -> str:
        if not core_facts:
            return ""
        # Filter by category registry when provided (skip non-L1 categories such as chitchat).
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

        # Select highest-importance recent facts within budget, then sort deterministically by fact_id
        # so consecutive turns produce byte-identical static prefixes for KV Prefix Cache hits.
        ranked = sorted(
            eligible,
            key=lambda f: (f.importance, f.updated_at),
            reverse=True,
        )
        selected: List[MemoryFact] = []
        used = 0
        for fact in ranked:
            line = f"- [{fact.category}] {fact.content}"
            cost = estimate_tokens(line)
            if selected and used + cost > self.budget.l1_profile_tokens:
                continue
            if not selected and cost > self.budget.l1_profile_tokens:
                break
            selected.append(fact)
            used += cost

        if not selected:
            return ""
        selected.sort(key=lambda f: f.fact_id)
        lines = "\n".join(f"- [{f.category}] {f.content}" for f in selected)
        return f"【核心用户画像（稳定长期事实）】\n{lines}"

    def _render_l3_recalled(self, recalled: List[RetrievedMemory]) -> str:
        if not recalled:
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
            cost = estimate_tokens(line)
            if lines and used + cost > self.budget.l3_recall_tokens:
                break
            lines.append(line)
            used += cost
        if not lines:
            return ""
        return "【相关长期记忆召回】\n" + "\n".join(lines)

    def _render_l3_skills(self, skills: List[MemorySkill]) -> str:
        if not skills:
            return ""
        import json as _json

        lines: List[str] = []
        for s in skills:
            try:
                steps = _json.loads(s.steps_json)
                step_text = " -> ".join(str(x) for x in steps[:5]) if isinstance(steps, list) else str(steps)
            except Exception:
                step_text = s.steps_json
            pre = f"（前置: {s.prerequisites}）" if s.prerequisites else ""
            lines.append(
                f"- [skill:{s.skill_id} | 工具={s.tool_preference} | 成功{s.success_count}次] "
                f"{s.name}{pre}: {step_text}"
            )
        return "【可复用的已学技能（Skill，按需参考执行）】\n" + "\n".join(lines)

    def _apply_ocr_budget(
        self,
        user_content: Union[str, List[Dict[str, Any]]],
        stats: BuildStats,
    ) -> Union[str, List[Dict[str, Any]]]:
        if isinstance(user_content, list):
            return user_content
        text = str(user_content)
        budget = self.budget.l3_desktop_ocr_tokens
        if estimate_tokens(text) <= budget:
            return text
        stats.ocr_truncated = True
        return truncate_ocr_smart(text, budget)
