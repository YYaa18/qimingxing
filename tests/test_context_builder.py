from __future__ import annotations

from brain.config import ContextBudgetConfig
from brain.context_builder import ContextBuilder, estimate_message_tokens
from brain.context_store import ContextStore
from brain.memory.models import MemoryFact, RetrievedMemory
from protocol.models import ContextWindowChangedPayload


def test_prefix_cache_static_prefix_byte_identical_across_turns() -> None:
    builder = ContextBuilder()
    ctx_store = ContextStore()
    ctx_store.update_window(
        ContextWindowChangedPayload(app_name="VS Code", window_title="hub.py", scene="coding")
    )

    facts = [
        MemoryFact(fact_id="fact_b", category="tech_stack", content="主力使用 Python 3.12", importance=0.9),
        MemoryFact(fact_id="fact_a", category="preference", content="喜欢直接给核心结论", importance=0.8),
    ]

    # Turn 1
    b1 = builder.build(
        user_text="你好，帮我看看这段异步代码",
        user_content="你好，帮我看看这段异步代码",
        core_facts=facts,
        rolling_summary="",
        recent_messages=[],
        recalled_memories=[
            RetrievedMemory(
                item_id="ep_1",
                item_type="episode",
                content="昨天排查过 FastAPI WebSocket 断线",
                category="conversation",
                importance=0.6,
                score=0.8,
                updated_at=100.0,
            )
        ],
        context_store=ctx_store,
    )

    # Turn 2: dynamic tail (desktop window, user text, recalled memories) changed,
    # while L0 + L1 core facts stay identical -> static system prefix MUST be byte-identical!
    ctx_store.update_window(
        ContextWindowChangedPayload(app_name="Terminal", window_title="pytest -v", scene="coding")
    )
    b2 = builder.build(
        user_text="再帮我看下第二个问题",
        user_content="再帮我看下第二个问题",
        core_facts=list(reversed(facts)),  # reversed input order still sorts stably by fact_id
        rolling_summary="前面讨论了异步代码结构。",
        recent_messages=[{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好！"}],
        recalled_memories=[],
        context_store=ctx_store,
    )

    assert b1.messages[0]["content"] == b2.messages[0]["content"]
    assert b1.stats.static_prefix_hash == b2.stats.static_prefix_hash
    # L3 dynamic memories & desktop context are placed in tail user message, not L0/L1 prefix
    assert "昨天排查过 FastAPI WebSocket 断线" in b1.messages[-1]["content"]
    assert "昨天排查过 FastAPI WebSocket 断线" not in b1.messages[0]["content"]


def test_token_budget_hard_cap_with_15k_ocr_and_20_turns() -> None:
    budget = ContextBudgetConfig(
        l1_profile_tokens=200,
        l2_summary_tokens=150,
        l2_recent_tokens=600,
        l3_recall_tokens=200,
        l3_desktop_ocr_tokens=400,
        total_max_tokens=1800,
    )
    builder = ContextBuilder(budget)
    ctx_store = ContextStore()

    huge_ocr = "\n".join(
        [f"HEAD_LINE_{i}: ZeroDivisionError Traceback at module_{i}.py" for i in range(40)]
        + ["MIDDLE_NOISE_LINE " * 40 for _ in range(80)]
        + [f"TAIL_LINE_{i}: final exit code 1" for i in range(40)]
    )
    assert len(huge_ocr) > 15000

    long_history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"第{i}轮较长的历史对话内容 " * 15}
        for i in range(40)
    ]

    built = builder.build(
        user_text="帮我分析这个超长报错",
        user_content=f"帮我分析这个超长报错\n【当前屏幕 OCR 提取内容】\n```text\n{huge_ocr}\n```",
        core_facts=[MemoryFact(fact_id="f1", category="fact", content="用户使用 macOS")],
        rolling_summary="早期摘要信息",
        recent_messages=long_history,
        recalled_memories=[],
        context_store=ctx_store,
    )

    assert built.stats.ocr_truncated is True
    assert built.stats.total_tokens <= budget.total_max_tokens
    # L0 system prompt & head/tail of OCR are preserved
    assert "启明星" in built.messages[0]["content"]
    tail_text = built.messages[-1]["content"]
    assert "HEAD_LINE_0" in tail_text
    assert "TAIL_LINE_" in tail_text
    assert sum(estimate_message_tokens(m) for m in built.messages) <= budget.total_max_tokens


def test_desktop_focus_denoising_and_two_view_rendering() -> None:
    clock = [1000.0]
    store = ContextStore(clock=lambda: clock[0], min_transient_seconds=3.0)

    store.update_window(ContextWindowChangedPayload(app_name="VS Code", window_title="hub.py", scene="coding"))
    clock[0] += 15.0
    # 2-second flicker to Finder (< 3s threshold)
    store.update_window(ContextWindowChangedPayload(app_name="Finder", window_title="Downloads", scene="desktop"))
    clock[0] += 2.0
    store.update_window(ContextWindowChangedPayload(app_name="VS Code", window_title="hub.py", scene="coding"))

    timeline = store.get_timeline()
    assert len(timeline) == 1
    assert timeline[0].app_name == "VS Code"

    builder = ContextBuilder()
    b_chat = builder.build(
        user_text="Python 的 asyncio.gather 怎么用？",
        user_content="Python 的 asyncio.gather 怎么用？",
        core_facts=[],
        rolling_summary="",
        recent_messages=[],
        recalled_memories=[],
        context_store=store,
    )
    assert b_chat.stats.timeline_expanded is False
    assert "最近桌面活动时间线" not in b_chat.messages[-1]["content"]
    assert "当前焦点: VS Code" in b_chat.messages[-1]["content"]

    b_activity = builder.build(
        user_text="我刚才这会儿主要在忙什么？",
        user_content="我刚才这会儿主要在忙什么？",
        core_facts=[],
        rolling_summary="",
        recent_messages=[],
        recalled_memories=[],
        context_store=store,
    )
    assert b_activity.stats.timeline_expanded is True
    assert "最近桌面活动时间线" in b_activity.messages[-1]["content"]


def test_sanitize_recent_messages_coalesces_failed_turns() -> None:
    from brain.context_builder import sanitize_recent_messages

    history = [
        {"role": "assistant", "content": "开场"},
        {"role": "user", "content": "失败的一轮"},
        {"role": "user", "content": "下一轮"},
        {"role": "assistant", "content": "回答"},
        {"role": "user", "content": "还没回答的当前轮"},
    ]
    cleaned = sanitize_recent_messages(history)
    assert [m["role"] for m in cleaned] == ["user", "assistant"]
    assert cleaned[0]["content"] == "失败的一轮\n\n下一轮"

    builder = ContextBuilder(ContextBudgetConfig(total_max_tokens=4000))
    built = builder.build(
        user_text="现在的问题",
        user_content="现在的问题",
        core_facts=[],
        rolling_summary="",
        recent_messages=history,
        recalled_memories=[],
        context_store=ContextStore(),
    )
    assert built.messages[-1]["role"] == "user"
    assert str(built.messages[-1]["content"]).endswith("现在的问题")
    roles = [m["role"] for m in built.messages]
    assert roles[0] == "system"
    for left, right in zip(roles, roles[1:]):
        assert not (left == "user" and right == "user")
    history_roles = roles[1:-1]
    assert not history_roles or history_roles[0] == "user"
