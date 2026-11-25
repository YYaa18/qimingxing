from __future__ import annotations

from brain.config import AppConfig, ContextBudgetConfig, ProfileConfig
from brain.context_budget import estimate_tools_tokens, resolve_context_budget
from brain.context_builder import ContextBuilder, TokenMeter
from brain.context_store import ContextStore
from brain.memory.models import MemoryFact
from brain.prompts import BASE_SYSTEM_PROMPT
from brain.tools.executor import ToolExecutor


def _profile(**kwargs) -> ProfileConfig:
    return ProfileConfig(
        name="p",
        label="P",
        provider="openai_compatible",
        model=kwargs.pop("model", "deepseek-flash"),
        provider_id=kwargs.pop("provider_id", "deepseek"),
        **kwargs,
    )


def _config(tier: int = 32000) -> AppConfig:
    cfg = AppConfig()
    cfg.context_budget = ContextBudgetConfig(input_tier_tokens=tier, request_max_tokens=8192)
    return cfg


def test_budget_uses_window_quarter_and_reserved_output() -> None:
    cfg = _config()
    budget = resolve_context_budget(
        profile=_profile(context_window=1_048_576, max_output_tokens=393_216),
        config=cfg,
        system_prompt=BASE_SYSTEM_PROMPT,
        tools_schema=[],
        has_facts=True,
        has_summary=True,
    )
    assert budget.total_input == 32_000
    assert budget.reserved_output == 8_192
    assert budget.l1_cap >= 600
    assert budget.summary_cap >= 1000
    assert budget.recent_cap >= 3000
    assert budget.l3_total_cap <= int(budget.total_input * cfg.context_budget.l3_total_ratio)


def test_budget_guards_max_output_equal_to_window() -> None:
    cfg = _config()
    budget = resolve_context_budget(
        profile=_profile(context_window=32768, max_output_tokens=32768),
        config=cfg,
        system_prompt=BASE_SYSTEM_PROMPT,
        tools_schema=[],
        has_facts=False,
        has_summary=False,
    )
    assert budget.reserved_output == 8_192
    assert budget.total_input == 8_192
    assert budget.total_input > 0


def test_budget_tier_64k_and_tools_subtraction() -> None:
    cfg = _config(tier=64000)
    tools = [{"type": "function", "function": {"name": f"t{i}", "description": "x" * 200}} for i in range(30)]
    budget = resolve_context_budget(
        profile=_profile(context_window=1_048_576, max_output_tokens=65_536),
        config=cfg,
        system_prompt=BASE_SYSTEM_PROMPT,
        tools_schema=tools,
        has_facts=True,
        has_summary=True,
    )
    assert budget.total_input == 64_000
    assert budget.tools_tokens > 0
    assert budget.available == budget.total_input - budget.l0_tokens - budget.tools_tokens


def test_builder_uses_resolved_budget_and_calibrates() -> None:
    cfg = _config()
    profile = _profile(context_window=1_048_576, max_output_tokens=393_216)
    resolved = resolve_context_budget(
        profile=profile,
        config=cfg,
        system_prompt=BASE_SYSTEM_PROMPT,
        tools_schema=[],
        has_facts=True,
        has_summary=True,
    )
    builder = ContextBuilder(cfg.context_budget)
    store = ContextStore()
    built = builder.build(
        user_text="继续刚才的代码任务",
        user_content="继续刚才的代码任务",
        core_facts=[
            MemoryFact(fact_id="f1", category="tech_stack", content="使用 FastAPI + SQLite WAL，主力 Python 3.12")
        ],
        rolling_summary="【已完成】完成了上下文预算改造。\n【关键文件】brain/context_builder.py",
        recent_messages=[
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"第{i}轮对话 " * 40}
            for i in range(40)
        ],
        recalled_memories=[],
        context_store=store,
        resolved_budget=resolved,
        tools_tokens=1000,
    )
    assert built.stats.resolved_total_input == 32_000
    assert built.stats.recent_budget > 2000
    assert built.stats.total_with_tools <= 32_000
    assert built.stats.calibration_scale == 1.0

    builder.observe_prompt_usage(1000, 1300)
    assert 1.0 < builder.meter.scale <= 1.8
    assert isinstance(estimate_tools_tokens([{"type": "function", "function": {"name": "x"}}]), int)


def test_tool_observation_folding_keeps_latest_outputs() -> None:
    executor = ToolExecutor(None)  # only the pure folding helper is exercised
    messages = [
        {"role": "user", "content": "start"},
        *[
            {"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 5000}
            for i in range(5)
        ],
    ]
    folded = executor._fold_old_tool_outputs(messages, max_tokens=2000, keep_last=2)
    assert "已折叠" in folded[1]["content"]
    assert "已折叠" in folded[2]["content"]
    assert "已折叠" in folded[3]["content"]
    assert folded[4]["content"] == "x" * 5000
    assert folded[5]["content"] == "x" * 5000
