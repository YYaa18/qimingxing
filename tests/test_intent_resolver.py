from __future__ import annotations

from brain.intent_resolver import (
    CAPABILITY_CATALOG,
    build_capability_catalog,
    build_intent_hint,
    resolve_intent,
)


def test_vague_browser_request_maps_to_browser_auto() -> None:
    hint = resolve_intent("试一下浏览器能力")
    assert hint.intent == "browser_auto"
    assert "browser_auto" in hint.candidate_packs
    assert hint.confidence >= 0.75


def test_browser_url_and_page_action_map_to_browser_auto() -> None:
    for text in (
        "用浏览器打开 example.com 看标题",
        "打开 https://example.com 并截图",
        "登录后台把表单填了",
    ):
        hint = resolve_intent(text)
        assert hint.intent == "browser_auto", text
        assert "browser_auto" in hint.candidate_packs


def test_web_table_to_xlsx_keeps_both_candidates() -> None:
    hint = resolve_intent("把网页里的表格抓下来存成xlsx")
    assert "browser_auto" in hint.candidate_packs
    assert "doc_office" in hint.candidate_packs


def test_doc_attachment_and_rename_maps_to_doc_office() -> None:
    text = "把桌面这 4 个文件按 Excel 序号重命名并核对缺 PDF"
    hint = resolve_intent(text, attachments=["机票-喂给AI.xlsx", "a.pdf"])
    assert hint.intent == "doc_office"
    assert "附件 .xlsx .pdf" in " ".join(hint.signals)


def test_macos_web_shell_and_media_hints() -> None:
    assert resolve_intent("明天下午提醒我交材料").intent == "macos_office"
    assert resolve_intent("查一下今天的科技新闻").intent == "web_research"
    assert resolve_intent("在终端跑一下 pytest").intent == "dev_ops"
    assert resolve_intent("帮我找几张横版壁纸图片").intent == "media_studio"


def test_general_chat_produces_no_hint() -> None:
    hint = resolve_intent("你好，今天心情怎么样")
    assert hint.candidate_packs == ()
    assert build_intent_hint("你好，今天心情怎么样") == ""


def test_catalog_and_hint_are_routing_context_only() -> None:
    catalog = build_capability_catalog()
    assert "browser_auto" in catalog
    assert "doc_office" in catalog
    assert "不构成授权" in catalog
    for pack, _ in CAPABILITY_CATALOG:
        assert pack in catalog

    hint = build_intent_hint("试一下浏览器能力")
    assert "browser_auto" in hint
    assert "不构成授权" in hint
    assert "allowed_domains" not in hint


def test_hint_is_injected_as_l3_section_not_system_prefix() -> None:
    from brain.context_builder import ContextBuilder
    from brain.context_store import ContextStore
    from brain.config import ContextBudgetConfig

    builder = ContextBuilder(ContextBudgetConfig(total_max_tokens=4000))
    built = builder.build(
        user_text="试一下浏览器能力",
        user_content="试一下浏览器能力",
        core_facts=[],
        rolling_summary="",
        recent_messages=[],
        recalled_memories=[],
        context_store=ContextStore(),
        extra_l3_sections=[
            build_capability_catalog(),
            build_intent_hint("试一下浏览器能力"),
        ],
    )
    assert built.messages[0]["role"] == "system"
    assert "【能力目录" not in built.messages[0]["content"]
    assert "【意图提示" not in built.messages[0]["content"]
    tail = built.messages[-1]["content"]
    assert "browser_auto" in tail
    assert "不构成授权" in tail
