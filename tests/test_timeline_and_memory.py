from __future__ import annotations

from brain.context_store import ContextStore
from brain.history_store import InMemorySlidingWindowStore, MemoryStore
from protocol.models import ContextWindowChangedPayload


def test_context_store_merged_timeline_noise_filter_and_privacy_mask() -> None:
    clock_now = [1000.0]
    store = ContextStore(
        clock=lambda: clock_now[0],
        max_timeline_entries=5,
        timeline_window_seconds=1800,
        min_transient_seconds=2.0,
    )

    # First work window: VS Code (stays 10s)
    store.update_window(
        ContextWindowChangedPayload(app_name="VS Code", window_title="hub.py", scene="coding")
    )
    clock_now[0] += 10.0
    # Same window updates should merge
    store.update_window(
        ContextWindowChangedPayload(app_name="VS Code", window_title="hub.py", scene="coding")
    )

    # Transient flicker (< 2s): Spotlight
    clock_now[0] += 5.0
    store.update_window(
        ContextWindowChangedPayload(app_name="Spotlight", window_title="Search", scene="desktop")
    )
    clock_now[0] += 0.8
    # Back to VS Code -> Spotlight (<2s) should be dropped and merged back into VS Code
    store.update_window(
        ContextWindowChangedPayload(app_name="VS Code", window_title="hub.py", scene="coding")
    )

    # Switch to Chrome (stays 20s)
    clock_now[0] += 20.0
    store.update_window(
        ContextWindowChangedPayload(app_name="Google Chrome", window_title="FastAPI Docs", scene="browser")
    )

    # Switch to Qimingxing self window -> updates current_window, preserves last_non_self_window, doesn't pollute timeline
    clock_now[0] += 5.0
    store.update_window(
        ContextWindowChangedPayload(
            app_name="Google Chrome",
            window_title="启明星 · Qimingxing MVP 2.0",
            scene="browser",
            is_self_window=True,
        )
    )
    assert store.get_snapshot().last_non_self_window is not None
    assert store.get_snapshot().last_non_self_window.window_title == "FastAPI Docs"

    # Privacy masked window
    clock_now[0] += 10.0
    store.update_window(
        ContextWindowChangedPayload(
            app_name="[隐私窗口已隐藏]",
            window_title="[隐私窗口已隐藏]",
            scene="privacy",
            privacy_masked=True,
        )
    )

    timeline = store.get_timeline()
    assert len(timeline) == 3
    assert timeline[0].app_name == "VS Code"
    assert timeline[1].app_name == "Google Chrome"
    assert timeline[2].app_name == "[隐私窗口已隐藏]"

    prompt_ctx = store.render_for_prompt()
    assert "最近桌面活动时间线" in prompt_ctx
    assert "VS Code | hub.py" in prompt_ctx
    assert "Google Chrome | FastAPI Docs" in prompt_ctx


def test_memory_store_sliding_window_and_clear() -> None:
    mem: MemoryStore = InMemorySlidingWindowStore(max_messages=4)
    for i in range(6):
        mem.append_turn("s1", "user" if i % 2 == 0 else "assistant", f"msg-{i}", scene_summary="VS Code")

    msgs = mem.get_messages("s1")
    assert len(msgs) == 4
    assert msgs[0]["content"] == "msg-2"
    turns = mem.get_turns("s1")
    assert turns[-1].scene_summary == "VS Code"
    assert turns[-1].ts > 0

    mem.clear("s1")
    assert mem.get_messages("s1") == []
