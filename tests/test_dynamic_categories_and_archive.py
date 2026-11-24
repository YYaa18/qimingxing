from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List

from fastapi.testclient import TestClient

from brain.config import AppConfig, MemoryExtractionConfig, ProfileConfig
from brain.context_builder import ContextBuilder
from brain.context_store import ContextStore
from brain.llm_router import LLMRouter
from brain.main import create_app
from brain.memory.cli import main as cli_main
from brain.memory.extractor import MAX_DYNAMIC_CATEGORIES, MemoryExtractor
from brain.memory.models import MemoryCategory, MemoryFact, MessageRecord, new_fact_id
from brain.memory.retrieval import MemoryRetriever
from brain.memory.sqlite_store import SQLiteMemoryStore


class DynamicCatMockLLM(LLMRouter):
    def __init__(self, config: AppConfig, payload: str) -> None:
        super().__init__(config)
        self.payload = payload

    async def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        profile_name: str | None = None,
    ) -> AsyncIterator[str]:
        yield self.payload


async def test_dynamic_category_creation_and_l1_filtering(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "dyn_cat.db")
    await store.initialize()

    cats = {c.category_key: c for c in await store.list_categories()}
    assert {"preference", "project", "tech_stack", "chitchat"}.issubset(cats.keys())

    # LLM dynamically creates a new category 'fitness_log' + records a chitchat_summary
    llm_json = json.dumps(
        {
            "new_categories": [
                {
                    "category_key": "fitness_log",
                    "display_name": "健身打卡",
                    "description": "用户的日常健身、跑步与力量训练记录",
                    "tier": "episodic",
                    "inject_to_l1": False,
                }
            ],
            "mutations": [
                {
                    "op": "ADD",
                    "category": "fitness_log",
                    "content": "用户今天完成了 5 公里慢跑",
                    "importance": 0.75,
                    "target_fact_id": None,
                }
            ],
            "episode_summary": None,
            "chitchat_summary": "用户讲了一个关于程序员电脑好的冷笑话",
        },
        ensure_ascii=False,
    )
    cfg = AppConfig(
        default_profile="fast_mock",
        profiles={"fast_mock": ProfileConfig(name="fast_mock", label="Fast", provider="mock", model="m")},
    )
    extractor = MemoryExtractor(
        store,
        DynamicCatMockLLM(cfg, llm_json),
        config=MemoryExtractionConfig(enabled=True, profile="fast_mock"),
    )
    sess = await store.get_or_create_active_session()
    await extractor.extract_from_messages(
        sess.session_id,
        [MessageRecord(1, sess.session_id, "t1", "user", "新建一个健身打卡分类，记录我今天跑了5公里")],
    )

    new_cat = await store.get_category("fitness_log")
    assert new_cat is not None
    assert new_cat.display_name == "健身打卡"
    assert new_cat.created_by == "llm"
    assert new_cat.inject_to_l1 is False

    # Verify ContextBuilder respects inject_to_l1=False (keeps fitness_log out of L1 static prefix)
    core_fact = await store.add_fact(
        MemoryFact(fact_id=new_fact_id(), category="tech_stack", content="主力使用 Python 3.12", importance=0.9)
    )
    all_facts = await store.list_facts(status="active")
    cats_map = {c.category_key: c for c in await store.list_categories()}

    builder = ContextBuilder()
    built = builder.build(
        user_text="帮我看个异步 bug",
        user_content="帮我看个异步 bug",
        core_facts=all_facts,
        rolling_summary="",
        recent_messages=[],
        recalled_memories=[],
        context_store=ContextStore(),
        categories=cats_map,
    )
    assert core_fact.content in built.messages[0]["content"]
    assert "5 公里慢跑" not in built.messages[0]["content"]

    # Chitchat episode was persisted cleanly
    chitchat_eps = await store.list_episodes(kind="chitchat")
    assert len(chitchat_eps) == 1
    assert "程序员" in chitchat_eps[0].summary
    await store.close()


async def test_category_weighted_retrieval_and_chitchat_recall(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "weighted.db")
    await store.initialize()

    sess = await store.get_or_create_active_session()
    # Add a core tech fact and a hot joke message mentioning '电脑'
    await store.add_fact(
        MemoryFact(
            fact_id="fact_core_pc",
            category="tech_stack",
            content="开发电脑运行 macOS Python 3.12 异步架构",
            importance=0.85,
        )
    )
    await store.append_message(
        session_id=sess.session_id,
        turn_id="t_joke",
        role="assistant",
        content="程序员笑话：在我电脑上明明是好的！",
    )

    retriever = MemoryRetriever(store)

    # Work query: core fact ranks above chitchat/message
    work_hits, _ = await retriever.search("我的电脑架构是什么？", top_k=5)
    assert work_hits[0].item_id == "fact_core_pc"

    # Explicit chitchat/joke recall query: finds the joke even after starting a new session
    await store.create_new_session(archive_previous=True)
    joke_hits, _ = await retriever.search("上次那个程序员笑话再讲一遍", top_k=5)
    assert any("在我电脑上明明是好的" in h.content for h in joke_hits)
    await store.close()


async def test_hot_cold_message_archiving_and_snapshot_roundtrip(tmp_path: Path) -> None:
    db1 = tmp_path / "archive1.db"
    store1 = SQLiteMemoryStore(db1)
    await store1.initialize()

    s1 = await store1.get_or_create_active_session()
    for i in range(6):
        await store1.append_message(
            session_id=s1.session_id,
            turn_id=f"t_{i}",
            role="user",
            content=f"旧会话消息编号 {i}：测试热表到冷归档表分离",
        )
    s2 = await store1.create_new_session(archive_previous=True)
    for i in range(4):
        await store1.append_message(
            session_id=s2.session_id,
            turn_id=f"t2_{i}",
            role="user",
            content=f"新会话消息编号 {i}：保留在热表里",
        )

    hot_before, cold_before = await store1.count_messages()
    assert (hot_before, cold_before) == (10, 0)

    moved = await store1.archive_old_messages(keep_latest=5, batch_size=10)
    assert moved == 5

    hot_after, cold_after = await store1.count_messages()
    assert (hot_after, cold_after) == (5, 5)

    # Archived messages are removed from hot FTS index
    fts_old = await store1.fts_search('"旧会话消息编号" AND "0"')
    assert not any(it == "message" and iid == "1" for iid, it, _ in fts_old)

    # Custom category + archived messages survive JSONL export -> import roundtrip
    await store1.register_category(
        MemoryCategory(
            category_key="reading_notes",
            display_name="读书笔记",
            tier="episodic",
            retrieval_weight=0.92,
            decay_rate=0.03,
            inject_to_l1=False,
            created_by="cli",
        )
    )
    snap = tmp_path / "snap_v31.jsonl"
    await store1.export_snapshot(snap)
    await store1.close()

    db2 = tmp_path / "archive2.db"
    store2 = SQLiteMemoryStore(db2)
    await store2.initialize()
    await store2.import_snapshot(snap)
    assert await store2.count_messages() == (5, 5)
    restored_cat = await store2.get_category("reading_notes")
    assert restored_cat is not None
    assert restored_cat.display_name == "读书笔记"
    await store2.close()


def test_cli_categories_archive_and_debug_endpoint(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "cli_v31.db"
    assert (
        cli_main(
            [
                "--db",
                str(db_path),
                "categories",
                "--add",
                "game_tips",
                "--name",
                "游戏攻略",
                "--tier",
                "episodic",
                "--weight",
                "0.9",
            ]
        )
        == 0
    )
    assert "game_tips" in capsys.readouterr().out

    assert cli_main(["--db", str(db_path), "categories"]) == 0
    assert "游戏攻略" in capsys.readouterr().out

    assert cli_main(["--db", str(db_path), "archive", "--keep", "10", "--batch", "5"]) == 0
    assert "Archived 0 message(s)" in capsys.readouterr().out

    cfg = AppConfig(
        default_profile="mock_local",
        profiles={"mock_local": ProfileConfig(name="mock_local", label="Mock", provider="mock", model="m")},
    )
    cfg.memory.sqlite_path = str(db_path)
    app = create_app(config=cfg)
    with TestClient(app) as client:
        dbg = client.get("/api/memory/debug").json()
        assert "hot_messages_count" in dbg
        assert "archived_messages_count" in dbg
        assert any(c["category_key"] == "game_tips" for c in dbg["categories"])


async def test_p3_01_llm_category_l1_forced_false_and_soft_cap(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "p3_01.db")
    await store.initialize()

    cfg = AppConfig(
        default_profile="fast_mock",
        profiles={"fast_mock": ProfileConfig(name="fast_mock", label="Fast", provider="mock", model="m")},
    )
    sess = await store.get_or_create_active_session()

    # Even if LLM explicitly outputs inject_to_l1=True + tier="core", backend must force inject_to_l1=False
    sneaky_json = json.dumps(
        {
            "new_categories": [
                {
                    "category_key": "secret_hobby",
                    "display_name": "小众爱好",
                    "description": "日常兴趣爱好",
                    "tier": "core",
                    "inject_to_l1": True,
                }
            ],
            "mutations": [
                {
                    "op": "ADD",
                    "category": "secret_hobby",
                    "content": "用户喜欢收藏机械键盘轴体",
                    "importance": 0.7,
                    "target_fact_id": None,
                }
            ],
            "episode_summary": None,
            "chitchat_summary": None,
        },
        ensure_ascii=False,
    )
    extractor1 = MemoryExtractor(
        store,
        DynamicCatMockLLM(cfg, sneaky_json),
        config=MemoryExtractionConfig(enabled=True, profile="fast_mock"),
    )
    await extractor1.extract_from_messages(
        sess.session_id,
        [MessageRecord(1, sess.session_id, "t1", "user", "记住我喜欢收藏机械键盘轴体")],
    )
    cat = await store.get_category("secret_hobby")
    assert cat is not None
    assert cat.created_by == "llm"
    assert cat.inject_to_l1 is False

    # Seed categories up to MAX_DYNAMIC_CATEGORIES and verify soft cap blocks overflow_cat
    existing = await store.list_categories()
    idx = 0
    while len(existing) + idx < MAX_DYNAMIC_CATEGORIES:
        await store.register_category(
            MemoryCategory(
                category_key=f"custom_cat_{idx}",
                display_name=f"自定义_{idx}",
                tier="episodic",
                inject_to_l1=False,
                created_by="cli",
            )
        )
        idx += 1
    assert len(await store.list_categories()) == MAX_DYNAMIC_CATEGORIES

    overflow_json = json.dumps(
        {
            "new_categories": [
                {
                    "category_key": "overflow_cat",
                    "display_name": "越界分类",
                    "description": "超出上限应被跳过",
                    "tier": "episodic",
                    "inject_to_l1": True,
                }
            ],
            "mutations": [
                {
                    "op": "ADD",
                    "category": "overflow_cat",
                    "content": "越界分类测试事实应安全回落到 fact",
                    "importance": 0.7,
                    "target_fact_id": None,
                }
            ],
            "episode_summary": None,
            "chitchat_summary": None,
        },
        ensure_ascii=False,
    )
    extractor2 = MemoryExtractor(
        store,
        DynamicCatMockLLM(cfg, overflow_json),
        config=MemoryExtractionConfig(enabled=True, profile="fast_mock"),
    )
    await extractor2.extract_from_messages(
        sess.session_id,
        [MessageRecord(2, sess.session_id, "t2", "user", "新建一个越界分类并记下来")],
    )
    assert await store.get_category("overflow_cat") is None
    assert len(await store.list_categories()) == MAX_DYNAMIC_CATEGORIES
    facts = await store.list_facts(status="active")
    fallback_fact = [f for f in facts if "越界分类测试事实" in f.content]
    assert len(fallback_fact) == 1
    assert fallback_fact[0].category == "fact"
    await store.close()


async def test_p3_02_archive_protects_active_unsummarized_messages(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "p3_02.db")
    await store.initialize()

    s1 = await store.get_or_create_active_session()
    for i in range(3):
        await store.append_message(
            session_id=s1.session_id,
            turn_id=f"s1_{i}",
            role="user",
            content=f"已归档会话消息 {i}",
        )

    s2 = await store.create_new_session(archive_previous=True)
    s2_msgs = []
    for i in range(5):
        m = await store.append_message(
            session_id=s2.session_id,
            turn_id=f"s2_{i}",
            role="user",
            content=f"活跃会话消息 {i}",
        )
        s2_msgs.append(m)

    # Mark only the first 2 messages in active session s2 as summarized
    watermark_id = s2_msgs[1].msg_id
    await store.update_session_summary(s2.session_id, "活跃会话前两条滚动摘要", watermark_id)

    moved = await store.archive_old_messages(keep_latest=1, batch_size=20)
    assert moved == 5  # 3 from archived s1 + 2 already-summarized from s2
    assert await store.count_messages() == (3, 5)

    remaining = await store.get_messages(s2.session_id, limit=20)
    assert [m.msg_id for m in remaining] == [m.msg_id for m in s2_msgs[2:]]

    # Calling archive again with keep_latest=1 must move 0 unsummarized active messages
    moved_again = await store.archive_old_messages(keep_latest=1, batch_size=20)
    assert moved_again == 0
    assert await store.count_messages() == (3, 5)
    await store.close()


async def test_p3_03_normal_query_caps_hot_messages_preserving_facts(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "p3_03.db")
    await store.initialize()

    sess = await store.get_or_create_active_session()
    for i in range(3):
        await store.add_fact(
            MemoryFact(
                fact_id=f"fact_arch_{i}",
                category="tech_stack",
                content=f"核心技术决策 {i}：采用纯异步架构实现记忆流水线",
                importance=0.9,
            )
        )
    for i in range(8):
        await store.append_message(
            session_id=sess.session_id,
            turn_id=f"msg_{i}",
            role="user",
            content=f"异步架构细节随口讨论第 {i} 条，重复提及异步架构设计方案",
        )

    retriever = MemoryRetriever(store)
    hits, _ = await retriever.search("异步架构设计方案是什么？", top_k=5)
    fact_ids = {h.item_id for h in hits if h.item_type == "fact"}
    msg_hits = [h for h in hits if h.item_type == "message"]
    assert fact_ids == {"fact_arch_0", "fact_arch_1", "fact_arch_2"}
    assert len(msg_hits) <= 1

    # Explicit chitchat/joke recall query lifts the message slot cap
    for i in range(3):
        await store.append_message(
            session_id=sess.session_id,
            turn_id=f"joke_{i}",
            role="assistant",
            content=f"程序员笑话第 {i} 弹：咖啡还没喝完需求又改了",
        )
    joke_hits, _ = await retriever.search("上次聊过的笑话有哪些", top_k=5)
    assert sum(1 for h in joke_hits if h.item_type == "message") >= 2
    await store.close()
