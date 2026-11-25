from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from brain.config import AppConfig, ContextBudgetConfig, ProfileConfig
from brain.context_builder import estimate_tokens
from brain.model_catalog import resolve_model_capabilities


@dataclass
class ResolvedContextBudget:
    window: int
    reserved_output: int
    tier_cap: int
    total_input: int
    l0_tokens: int
    tools_tokens: int
    available: int
    l1_cap: int
    summary_cap: int
    recent_cap: int
    recent_floor: int
    l3_total_cap: int
    recall_cap: int
    skills_cap: int
    attachments_cap: int
    desktop_cap: int
    tool_loop_max: int
    source: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def estimate_tools_tokens(tools_schema: Optional[List[Dict[str, Any]]]) -> int:
    if not tools_schema:
        return 0
    try:
        raw = json.dumps(tools_schema, ensure_ascii=False)
    except Exception:
        raw = str(tools_schema)
    # Schema JSON is mostly ASCII; the generic estimator already accounts for
    # mixed content. Add a small per-tool wrapper overhead.
    return estimate_tokens(raw) + 8 * len(tools_schema)


def resolve_context_budget(
    *,
    profile: ProfileConfig,
    config: AppConfig,
    system_prompt: str,
    tools_schema: Optional[List[Dict[str, Any]]] = None,
    has_facts: bool = False,
    has_summary: bool = False,
) -> ResolvedContextBudget:
    cfg: ContextBudgetConfig = config.context_budget
    caps = resolve_model_capabilities(
        profile.provider_id,
        profile.model,
        fallback_context_window=cfg.fallback_context_window,
        fallback_max_output=8192,
    )
    window = int(profile.context_window or caps.context_window or cfg.fallback_context_window)
    max_output = int(profile.max_output_tokens or caps.max_output_tokens or 0)
    request_max = int(profile.request_max_tokens or cfg.request_max_tokens or 8192)
    request_max = max(1024, request_max)
    reserved_output = max(1024, min(max_output or request_max, request_max))

    tier_cap = min(64000, max(16000, int(cfg.input_tier_tokens or 32000)))
    raw_target = min(tier_cap, max(8000, int(window * 0.25)))
    total_input = max(4096, min(raw_target, window - reserved_output - 2048))

    l0_tokens = estimate_tokens(system_prompt)
    tools_tokens = estimate_tools_tokens(tools_schema)
    available = max(512, total_input - l0_tokens - tools_tokens)

    l1_floor = min(cfg.l1_profile_floor_tokens, available) if has_facts else 0
    summary_floor = min(cfg.l2_summary_floor_tokens, available) if has_summary else 0
    recent_floor = min(cfg.l2_recent_floor_tokens, available)

    floors_total = l1_floor + summary_floor + recent_floor
    if floors_total > available:
        # Small windows cannot satisfy every floor; scale them down while
        # keeping the relative order. L3 gets nothing in this case.
        scale = available / max(1, floors_total)
        l1_cap = max(0, int(l1_floor * scale))
        summary_cap = max(0, int(summary_floor * scale))
        recent_cap = max(0, available - l1_cap - summary_cap)
        l3_total_cap = 0
        recent_floor = max(0, min(recent_floor, recent_cap))
    else:
        # Reserve the recent floor and give L1/summary their caps when room
        # allows. L3 only consumes what remains after those reservations.
        l1_cap = min(cfg.l1_profile_tokens, available - summary_floor - recent_floor) if has_facts else 0
        l1_cap = max(l1_cap, l1_floor)
        summary_cap = (
            min(cfg.l2_summary_tokens, available - l1_cap - recent_floor)
            if has_summary
            else 0
        )
        summary_cap = max(summary_cap, summary_floor)
        l3_total_cap = min(
            int(total_input * cfg.l3_total_ratio),
            max(0, available - l1_cap - summary_cap - recent_floor),
        )
        recent_cap = max(0, available - l1_cap - summary_cap - l3_total_cap)

    recall_cap = min(cfg.l3_recall_tokens, l3_total_cap)
    skills_cap = min(cfg.l3_skills_tokens, l3_total_cap)
    attachments_cap = min(cfg.l3_attachments_tokens, l3_total_cap)
    desktop_cap = min(cfg.l3_desktop_ocr_tokens, l3_total_cap)

    tool_loop_max = min(
        32000,
        max(4096, window - reserved_output - total_input - tools_tokens),
    )

    return ResolvedContextBudget(
        window=window,
        reserved_output=reserved_output,
        tier_cap=tier_cap,
        total_input=total_input,
        l0_tokens=l0_tokens,
        tools_tokens=tools_tokens,
        available=available,
        l1_cap=l1_cap,
        summary_cap=summary_cap,
        recent_cap=recent_cap,
        recent_floor=recent_floor,
        l3_total_cap=l3_total_cap,
        recall_cap=recall_cap,
        skills_cap=skills_cap,
        attachments_cap=attachments_cap,
        desktop_cap=desktop_cap,
        tool_loop_max=tool_loop_max,
        source=(
            caps.source
            if profile.capability_source in ("", "default")
            else profile.capability_source
        ),
    )
