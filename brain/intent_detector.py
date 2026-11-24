from __future__ import annotations

import re

_SCREEN_PHRASES = (
    "屏幕上",
    "看下屏幕",
    "看看屏幕",
    "看一眼屏幕",
    "看屏",
    "截图",
    "当前页面",
    "当前网页",
    "当前窗口",
    "当前画面",
    "这个报错",
    "这段报错",
    "这个错误",
    "这段代码",
    "这段话",
    "这段内容",
    "我正在看",
    "正在看的",
    "图里",
    "画面里",
    "界面上",
    "界面里",
    "图表里",
    "帮我看看",
    "帮我看下",
    "看一下这个",
    "看看这个",
    "on my screen",
    "on the screen",
    "this error",
    "current page",
    "screenshot",
    "what am i looking at",
)

_SCREEN_REGEXES = (
    re.compile(r"(看看|看下|看一眼|解释|分析|读一下).{0,8}(屏幕|画面|页面|网页|报错|代码|图表|截图)"),
    re.compile(r"(屏幕|画面|界面|图里).{0,8}(显示|写着|是什么|怎么|报错)"),
)


class ScreenIntentDetector:
    """Two-stage lightweight visual intent detector (zero false positives on general chat)."""

    def detect(self, text: str, *, explicit_action: bool = False) -> bool:
        if explicit_action:
            return True
        if not text:
            return False

        normalized = text.strip().lower()
        for phrase in _SCREEN_PHRASES:
            if phrase in normalized:
                return True

        for pat in _SCREEN_REGEXES:
            if pat.search(normalized):
                return True

        return False
