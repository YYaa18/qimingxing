from __future__ import annotations

import json

from brain.llm_router import parse_dsml_tool_calls


def test_parse_dsml_invoke_and_parameters() -> None:
    content = (
        '<｜｜DSML｜｜ calls>\n'
        '<｜｜DSML｜｜ invoke name="doc_inspect">\n'
        '<｜｜DSML｜｜ parameter name="path" string="true">~/Desktop/自然语言实测-7</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="max_rows" string="false">50</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n'
        '<｜｜DSML｜｜ invoke name="doc_match_rename">\n'
        '<｜｜DSML｜｜ parameter name="copy" string="false">true</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="files" string="false">["a.pdf", "b.pdf"]</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n'
        '</｜｜DSML｜｜ calls>'
    )
    cleaned, calls = parse_dsml_tool_calls(content)
    assert cleaned == ""
    assert [c.name for c in calls] == ["doc_inspect", "doc_match_rename"]
    first = json.loads(calls[0].arguments_json)
    assert first == {"path": "~/Desktop/自然语言实测-7", "max_rows": 50}
    second = json.loads(calls[1].arguments_json)
    assert second == {"copy": True, "files": ["a.pdf", "b.pdf"]}


def test_parse_dsml_keeps_real_text_and_ignores_plain_text() -> None:
    cleaned, calls = parse_dsml_tool_calls("结论：目录里只有 2 个 PDF。")
    assert cleaned.startswith("结论")
    assert calls == []

    content = (
        "先读取表格。\n"
        '<｜｜DSML｜｜ invoke name="doc_inspect">\n'
        '<｜｜DSML｜｜ parameter name="path" string="true">/tmp/a.xlsx</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>'
    )
    cleaned, calls = parse_dsml_tool_calls(content)
    assert cleaned == "先读取表格。"
    assert calls[0].name == "doc_inspect"
