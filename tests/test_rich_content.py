from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from brain.rich_content import clean_answer_and_sources, collect_media
from protocol.export_schema import build_protocol_schema
from protocol.models import MediaItem, OutputTurnEndPayload, SourceItem


def test_clean_gemini_redirect_sources_and_orphan_bullet() -> None:
    text = (
        "这是答案正文。\n\n参考来源：\n"
        "- [blog.google](https://vertexaisearch.cloud.google.com/grounding-api-redirect/VERY_LONG_TOKEN_1234567890)\n"
        "-"
    )
    cleaned, sources = clean_answer_and_sources(text)
    assert "参考来源" not in cleaned
    assert "vertexaisearch" not in cleaned
    assert cleaned == "这是答案正文。"
    assert len(sources) == 1
    assert sources[0]["domain"] == "blog.google"
    assert sources[0]["url"].startswith("https://vertexaisearch.cloud.google.com/")


def test_trailing_bare_urls_and_links_are_moved_into_sources() -> None:
    cleaned, sources = clean_answer_and_sources(
        "正文。\n\nhttps://example.com/a\n-\n[来源](https://example.com/b)\n---"
    )
    assert cleaned == "正文。"
    assert [s["url"] for s in sources] == ["https://example.com/a", "https://example.com/b"]


def test_illegal_schemes_are_filtered_and_duplicates_are_removed() -> None:
    text = (
        "正文\n\n参考来源：\n"
        "- [bad](javascript:alert(1))\n"
        "- [local](file:///tmp/a.txt)\n"
        "- [good](https://example.com/a)\n"
        "- [again](https://example.com/a)\n"
    )
    cleaned, sources = clean_answer_and_sources(text)
    assert cleaned == "正文"
    assert [s["url"] for s in sources] == ["https://example.com/a"]


def test_structured_sources_take_priority() -> None:
    cleaned, sources = clean_answer_and_sources(
        "答案",
        [{"type": "url_citation", "url_citation": {
            "url": "https://weather.com.cn/forecast",
            "title": "中央气象台",
        }}],
    )
    assert cleaned == "答案"
    assert sources[0]["title"] == "中央气象台"
    assert sources[0]["domain"] == "weather.com.cn"


def test_collect_media_image_metadata(tmp_path: Path) -> None:
    image_path = tmp_path / "preview.png"
    Image.new("RGB", (64, 48), (20, 30, 40)).save(image_path)
    items = collect_media([str(image_path), str(image_path), str(tmp_path / "missing.png")])
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "image"
    assert item["width"] == 64 and item["height"] == 48
    assert item["mime_type"] == "image/png"
    assert item["size_bytes"] > 0


def test_turn_end_payload_and_schema_are_in_sync() -> None:
    payload = OutputTurnEndPayload(
        sources=[SourceItem(title="Example", url="https://example.com", domain="example.com")],
        media=[MediaItem(media_id="m1", kind="image", title="a.png", local_path="/tmp/a.png")],
    )
    dumped = payload.model_dump()
    assert dumped["sources"][0]["url"] == "https://example.com"
    assert dumped["media"][0]["kind"] == "image"
    schema = build_protocol_schema()
    on_disk = json.loads(Path("protocol.schema.json").read_text(encoding="utf-8"))
    assert schema == on_disk


import sqlite3
import pytest
from brain.memory.sqlite_store import SQLiteMemoryStore


@pytest.mark.asyncio
async def test_message_sources_media_persist_and_old_db_upgrade(tmp_path: Path) -> None:
    old_db = tmp_path / "old_messages.db"
    conn = sqlite3.connect(old_db)
    conn.executescript(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY, started_at REAL NOT NULL,
            updated_at REAL NOT NULL, rolling_summary TEXT NOT NULL DEFAULT '',
            last_summarized_msg_id INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE messages (
            msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL, turn_id TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL, content TEXT NOT NULL,
            emotion TEXT NOT NULL DEFAULT 'neutral',
            desktop_context_summary TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    store = SQLiteMemoryStore(old_db)
    await store.initialize()
    sess = await store.get_or_create_active_session()
    sources = [{"title": "Example", "url": "https://example.com", "domain": "example.com"}]
    media = [{"media_id": "m1", "kind": "image", "title": "a.png", "local_path": "/tmp/a.png"}]
    await store.append_message(
        session_id=sess.session_id, turn_id="t1", role="assistant", content="答案",
        sources=sources, media=media,
    )
    msgs = await store.get_messages(sess.session_id, limit=10, oldest_first=True)
    assert msgs[-1].sources == sources
    assert msgs[-1].media == media
    with sqlite3.connect(old_db) as check:
        cols = {r[1] for r in check.execute("PRAGMA table_info(messages)")}
    assert {"sources_json", "media_json"}.issubset(cols)

    snapshot = tmp_path / "rich_snapshot.jsonl"
    await store.export_snapshot(snapshot)
    await store.close()
    imported_store = SQLiteMemoryStore(tmp_path / "rich_import.db")
    await imported_store.initialize()
    await imported_store.import_snapshot(snapshot)
    imported_msgs = await imported_store.get_messages(sess.session_id, limit=10, oldest_first=True)
    assert imported_msgs[-1].sources == sources
    assert imported_msgs[-1].media == media
    await imported_store.close()
