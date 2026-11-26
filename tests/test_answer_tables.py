from __future__ import annotations

import json

from fastapi.testclient import TestClient

from brain.config import AppConfig, ProfileConfig
from brain.llm_router import LLMCompletionResult, LLMRouter
from brain.main import create_app
from brain.rich_content import extract_table_media
from protocol.models import MediaItem, OutputTurnEndPayload


TABLE_ANSWER = (
    "**端口检查结果**\n\n"
    "| 检查项 | 结果 |\n"
    "| --- | --- |\n"
    "| 18790 | 已监听 |\n"
    "| 返回值 | 0 |"
)


class _TableRouter(LLMRouter):
    async def complete_with_tools(
        self,
        messages,
        tools,
        *,
        profile_name=None,
        on_text_delta=None,
        on_server_tool_event=None,
    ) -> LLMCompletionResult:
        if on_text_delta is not None:
            await on_text_delta(TABLE_ANSWER)
        return LLMCompletionResult(content=TABLE_ANSWER)


def test_extracts_gfm_table_as_virtual_media() -> None:
    text = (
        "结论：带外套。\n\n"
        "**晚间分时段**\n\n"
        "| 时段 | 气温 | 天气 |\n"
        "| --- | --- | --- |\n"
        "| 17–19 时 | 11–14℃ | 多云 |\n"
        "| 20–21 时 | 7–8℃ | 多云 |\n\n"
        "体感温度：偏冷。"
    )
    items = extract_table_media(text, turn_id="turn_table")
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "file"
    assert item["file_ext"] == "md-table"
    assert item["local_path"] == ""
    assert item["title"] == "晚间分时段"
    assert item["summary_badge"] == "2 行 × 3 列"
    assert item["preview_columns"] == ["时段", "气温", "天气"]
    assert item["preview_rows"][0] == ["17–19 时", "11–14℃", "多云"]
    assert item["media_id"].startswith("tbl_")

    payload = OutputTurnEndPayload(media=[MediaItem(**item)])
    assert payload.media[0].file_ext == "md-table"
    assert payload.media[0].preview_columns == ["时段", "气温", "天气"]


def test_extracts_multiple_tables_in_order_with_fallback_titles() -> None:
    text = (
        "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
        "中间说明\n\n"
        "| C | D |\n| :--- | ---: |\n| 3 | 4 |\n"
    )
    items = extract_table_media(text, turn_id="turn_multi")
    assert [item["title"] for item in items] == ["表格 1", "表格 2"]
    assert items[0]["preview_rows"] == [["1", "2"]]
    assert items[1]["preview_rows"] == [["3", "4"]]
    assert len({item["media_id"] for item in items}) == 2


def test_ignores_tables_inside_code_fences_and_cards() -> None:
    text = (
        "```markdown\n| A | B |\n| --- | --- |\n| 1 | 2 |\n```\n\n"
        "<<<CARD title=\"x\">>>\n| C | D |\n| --- | --- |\n| 3 | 4 |\n<<<END_CARD>>>\n\n"
        "| E | F |\n| --- | --- |\n| 5 | 6 |\n"
    )
    items = extract_table_media(text)
    assert len(items) == 1
    assert items[0]["preview_columns"] == ["E", "F"]


def test_handles_escaped_and_code_span_pipes() -> None:
    text = (
        "| 名称 | 说明 |\n"
        "| --- | --- |\n"
        "| a\\|b | `x|y` |\n"
    )
    items = extract_table_media(text)
    assert items[0]["preview_rows"][0] == ["a|b", "`x|y`"]


def test_requires_a_separator_row_and_body() -> None:
    assert extract_table_media("| A | B |\n| 1 | 2 |\n") == []
    assert extract_table_media("| A | B |\n| --- | --- |\n") == []
    assert extract_table_media("普通文本，没有表格。") == []


def test_caps_columns_rows_and_cell_length() -> None:
    header = "| " + " | ".join(f"c{i}" for i in range(12)) + " |"
    separator = "| " + " | ".join("---" for _ in range(12)) + " |"
    rows = [
        "| " + " | ".join([f"r{row}c{col}" for col in range(12)]) + " |"
        for row in range(25)
    ]
    items = extract_table_media("\n".join([header, separator, *rows]))
    item = items[0]
    assert len(item["preview_columns"]) == 10
    assert len(item["preview_rows"]) == 20
    assert item["summary_badge"] == "20+ 行 × 10+ 列"
    assert all(len(cell) <= 160 for row in item["preview_rows"] for cell in row)


def test_turn_end_carries_virtual_table_media(tmp_path) -> None:
    cfg = AppConfig(
        default_profile="mock_local",
        profiles={
            "mock_local": ProfileConfig(
                name="mock_local", label="Mock", provider="mock", model="mock-v1"
            )
        },
    )
    cfg.memory.sqlite_path = str(tmp_path / "tables.db")
    cfg.memory.extraction.enabled = False
    app = create_app(config=cfg, llm_router=_TableRouter(cfg))

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({
                "v": "1.0",
                "id": "h_table",
                "ts": 1,
                "source": "body",
                "type": "system.hello",
                "payload": {
                    "client_name": "body",
                    "role": "body",
                    "client_type": "body_macos",
                    "platform": "macos",
                },
            }))
            while json.loads(ws.receive_text())["type"] != "system.welcome":
                pass
            ws.send_text(json.dumps({
                "v": "1.0",
                "id": "t_table",
                "turn_id": "turn_table_ws",
                "ts": 2,
                "source": "body",
                "type": "input.text",
                "payload": {"text": "给我一个端口检查表格"},
            }))
            turn_end = None
            while turn_end is None:
                event = json.loads(ws.receive_text())
                if event["type"] == "output.turn_end":
                    turn_end = event
            media = turn_end["payload"]["media"]
            assert len(media) == 1
            assert media[0]["file_ext"] == "md-table"
            assert media[0]["title"] == "端口检查结果"
            assert media[0]["preview_rows"] == [["18790", "已监听"], ["返回值", "0"]]
