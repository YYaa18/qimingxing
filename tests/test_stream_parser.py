from __future__ import annotations

from brain.stream_parser import CardEvent, EmotionEvent, StreamParser, TextEvent


def _collect(parser: StreamParser, chunks: list[str]):
    events = []
    for c in chunks:
        events.extend(parser.feed(c))
    events.extend(parser.finish())
    return events


def _joined_text(events) -> str:
    return "".join(e.delta for e in events if isinstance(e, TextEvent))


def test_single_character_streaming_with_emotion_and_card() -> None:
    sample = (
        "[EMOTION:happy]\n"
        "先稳住血线再找输出窗口。\n"
        '<<<CARD title="红狮子城 Boss 提示" type="spoiler_steps">>>\n'
        "**第 1 级提示**\n"
        "- 留意场地金色印记\n"
        "<<<END_CARD>>>"
    )
    parser = StreamParser()
    events = _collect(parser, list(sample))

    emotions = [e for e in events if isinstance(e, EmotionEvent)]
    cards = [e for e in events if isinstance(e, CardEvent)]
    spoken = _joined_text(events)

    assert len(emotions) == 1
    assert emotions[0].emotion == "happy"
    assert "[EMOTION" not in spoken
    assert "<<<CARD" not in spoken
    assert "<<<END_CARD" not in spoken
    assert spoken.strip() == "先稳住血线再找输出窗口。"

    assert len(cards) == 1
    assert cards[0].title == "红狮子城 Boss 提示"
    assert cards[0].card_type == "spoiler_steps"
    assert "留意场地金色印记" in cards[0].content_markdown


def test_cross_chunk_split_tags_and_no_prefix_leak() -> None:
    chunks = [
        "[EMO",
        "TION:sur",
        "prised]太",
        "棒了！列表[1]",
        "和条件 a < b 都很正常。",
        "<<<CA",
        'RD type="markdown" title="配装对比">>>\n| 武器 | 伤害 |\n<<<END_',
        "CARD>>>",
    ]
    parser = StreamParser()
    all_events = []
    for ch in chunks:
        step_events = parser.feed(ch)
        for ev in step_events:
            if isinstance(ev, TextEvent):
                # Ensure partial prefix tags never leak mid-stream
                assert "[EMO" not in ev.delta
                assert "<<<CA" not in ev.delta
        all_events.extend(step_events)
    all_events.extend(parser.finish())

    emotions = [e for e in all_events if isinstance(e, EmotionEvent)]
    cards = [e for e in all_events if isinstance(e, CardEvent)]
    spoken = _joined_text(all_events)

    assert [e.emotion for e in emotions] == ["surprised"]
    assert spoken == "太棒了！列表[1]和条件 a < b 都很正常。"
    assert len(cards) == 1
    assert cards[0].title == "配装对比"
    assert cards[0].card_type == "markdown"
    assert "| 武器 | 伤害 |" in cards[0].content_markdown


def test_invalid_emotion_falls_back_to_neutral() -> None:
    parser = StreamParser()
    events = _collect(parser, ["[EMOTION:ecstatic]你好！"])
    emotions = [e for e in events if isinstance(e, EmotionEvent)]
    assert len(emotions) == 1
    assert emotions[0].emotion == "neutral"
    assert _joined_text(events) == "你好！"


def test_unclosed_card_auto_closes_on_eof() -> None:
    sample = (
        "[EMOTION:thinking]\n正在为你总结：\n"
        '<<<CARD title="未闭合测试" type="markdown">>>\n'
        "- 第一条建议\n- 第二条建议"
    )
    parser = StreamParser()
    events = _collect(parser, [sample[:35], sample[35:]])
    cards = [e for e in events if isinstance(e, CardEvent)]
    assert len(cards) == 1
    assert cards[0].title == "未闭合测试"
    assert "- 第一条建议" in cards[0].content_markdown
    assert "- 第二条建议" in cards[0].content_markdown


def test_double_bracket_false_positive_followed_by_real_tag() -> None:
    sample = "[[EMOTION:happy]你好<x<<<CARD title='t' type='markdown'>>>body<<<END_CARD>>>"
    parser = StreamParser()
    events = _collect(parser, list(sample))
    emotions = [e for e in events if isinstance(e, EmotionEvent)]
    cards = [e for e in events if isinstance(e, CardEvent)]
    spoken = _joined_text(events)

    assert [e.emotion for e in emotions] == ["happy"]
    assert spoken == "[你好<x"
    assert len(cards) == 1
    assert cards[0].content_markdown == "body"


def test_case_insensitive_whitespace_emotion_and_chinese_quotes() -> None:
    sample = (
        "[emotion : Happy ]\n"
        "口语先讲两句。\n"
        "<<<CARD type=‘spoiler_steps’ title=“Boss 提示”>>>\n"
        "- 第 1 步\n"
        "<<<end_card>>>"
    )
    parser = StreamParser()
    events = _collect(parser, [sample[i : i + 3] for i in range(0, len(sample), 3)])
    emotions = [e for e in events if isinstance(e, EmotionEvent)]
    cards = [e for e in events if isinstance(e, CardEvent)]
    spoken = _joined_text(events)

    assert [e.emotion for e in emotions] == ["happy"]
    assert spoken.strip() == "口语先讲两句。"
    assert len(cards) == 1
    assert cards[0].title == "Boss 提示"
    assert cards[0].card_type == "spoiler_steps"
    assert "- 第 1 步" in cards[0].content_markdown


def test_fenced_card_block_does_not_leak_stray_backticks() -> None:
    sample = (
        "[EMOTION:thinking]\n"
        "我看一下配置：\n"
        "```markdown\n"
        '<<<CARD title="配置表" type="markdown">>>\n'
        "| 字段 | 值 |\n"
        "<<<END_CARD>>>\n"
        "```\n"
        "正常普通代码：\n"
        "```python\nprint('ok')\n```"
    )
    parser = StreamParser()
    events = _collect(parser, list(sample))
    cards = [e for e in events if isinstance(e, CardEvent)]
    spoken = _joined_text(events)

    assert len(cards) == 1
    assert cards[0].title == "配置表"
    assert "| 字段 | 值 |" in cards[0].content_markdown
    assert "```markdown" not in spoken
    assert "print('ok')" in spoken
    assert spoken.count("```") == 2


def test_leading_space_after_emotion_tag_is_trimmed() -> None:
    parser = StreamParser()
    events = _collect(parser, ["[EMOTION:happy] ", "你好呀！"])
    spoken = _joined_text(events)
    assert spoken == "你好呀！"

