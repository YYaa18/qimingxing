from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List

from brain.config import AppConfig, MemoryExtractionConfig, ProfileConfig
from brain.context_store import ContextStore
from brain.llm_router import LLMError, LLMRouter
from brain.memory.cli import main as cli_main
from brain.memory.extractor import MemoryExtractor, has_explicit_memory_command
from brain.memory.models import MemoryFact, MessageRecord, new_fact_id
from brain.memory.sqlite_store import SQLiteMemoryStore
from protocol.models import ContextWindowChangedPayload


class MockExtractorLLM(LLMRouter):
    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self.responses: List[str] = []
        self.fail_mode = False

    async def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        profile_name: str | None = None,
    ) -> AsyncIterator[str]:
        if self.fail_mode:
            raise LLMError("LLM_TIMEOUT", "simulated timeout")
        if self.responses:
            yield self.responses.pop(0)
            return
        yield '{"mutations":[{"op":"NONE"}],"episode_summary":null}'


def _make_extractor(tmp_path: Path):
    cfg = AppConfig(
        default_profile="fast_mock",
        profiles={
            "fast_mock": ProfileConfig(
                name="fast_mock", label="Fast", provider="mock", model="mock-v1"
            )
        },
        memory=AppConfig().memory,
    )
    router = MockExtractorLLM(cfg)
    store = SQLiteMemoryStore(tmp_path / "ext.db")
    ext_cfg = MemoryExtractionConfig(
        enabled=True,
        profile="fast_mock",
        batch_turns=4,
        idle_seconds=60.0,
        persist_desktop_footprints=True,
        compress_trigger_turns=3,
        compress_batch_turns=2,
    )
    extractor = MemoryExtractor(store, router, config=ext_cfg)
    return store, router, extractor


async def test_conflict_resolution_add_update_delete_none_and_error_isolation(tmp_path: Path) -> None:
    store, router, extractor = _make_extractor(tmp_path)
    await store.initialize()
    sess = await store.get_or_create_active_session()

    # 1. NONE filters out small talk
    router.responses.append('{"mutations":[{"op":"NONE"}],"episode_summary":null}')
    res_none = await extractor.extract_from_messages(
        sess.session_id,
        [MessageRecord(1, sess.session_id, "t1", "user", "谢谢")],
    )
    assert res_none == []
    assert await store.list_facts(status="active") == []

    # 2. ADD initial Python 3.9 fact
    router.responses.append(
        json.dumps(
            {
                "mutations": [
                    {
                        "op": "ADD",
                        "category": "tech_stack",
                        "content": "项目 A 目前使用 Python 3.9",
                        "importance": 0.9,
                        "target_fact_id": None,
                    }
                ],
                "episode_summary": None,
            },
            ensure_ascii=False,
        )
    )
    await extractor.extract_from_messages(
        sess.session_id,
        [MessageRecord(2, sess.session_id, "t2", "user", "我现在主力语言是 Python 3.9，在写项目 A")],
    )
    active1 = await store.list_facts(status="active")
    assert len(active1) == 1
    old_id = active1[0].fact_id

    # 3. UPDATE supersedes old Python 3.9 fact with Python 3.12
    router.responses.append(
        json.dumps(
            {
                "mutations": [
                    {
                        "op": "UPDATE",
                        "category": "tech_stack",
                        "content": "项目 A 已升级使用 Python 3.12",
                        "importance": 0.95,
                        "target_fact_id": old_id,
                    }
                ],
                "episode_summary": None,
            },
            ensure_ascii=False,
        )
    )
    await extractor.extract_from_messages(
        sess.session_id,
        [MessageRecord(3, sess.session_id, "t3", "user", "我已经把项目 A 升级到 Python 3.12 了")],
    )
    old_fact = await store.get_fact(old_id)
    assert old_fact is not None and old_fact.status == "superseded"
    active2 = await store.list_facts(status="active")
    assert len(active2) == 1
    assert "3.12" in active2[0].content
    new_id = active2[0].fact_id

    # 4. Explicit remember/forget detection + DELETE soft-deletes
    assert has_explicit_memory_command("记住以后回答技术问题直接给核心结论") is True
    assert has_explicit_memory_command("忘掉我刚才说的测试代号 X") is True
    assert has_explicit_memory_command("今天天气不错") is False

    router.responses.append(
        json.dumps(
            {
                "mutations": [
                    {
                        "op": "DELETE",
                        "category": "tech_stack",
                        "content": "",
                        "importance": 0.5,
                        "target_fact_id": new_id,
                    }
                ],
                "episode_summary": None,
            }
        )
    )
    await extractor.extract_from_messages(
        sess.session_id,
        [MessageRecord(4, sess.session_id, "t4", "user", "忘掉刚才那条 Python 记录")],
    )
    deleted_fact = await store.get_fact(new_id)
    assert deleted_fact is not None and deleted_fact.status == "deleted"
    assert await store.list_facts(status="active") == []

    # 5. Background failure is isolated and never crashes caller
    router.fail_mode = True
    await extractor._safe_extract(
        sess.session_id,
        [MessageRecord(5, sess.session_id, "t5", "user", "记住一条会失败的提取")],
    )
    await store.close()


async def test_desktop_footprint_strictly_excludes_blacklisted_windows(tmp_path: Path) -> None:
    store, _router, extractor = _make_extractor(tmp_path)
    await store.initialize()

    clock = [1000.0]
    ctx = ContextStore(clock=lambda: clock[0], min_transient_seconds=2.0)

    ctx.update_window(ContextWindowChangedPayload(app_name="VS Code", window_title="qimingxing/hub.py", scene="coding"))
    clock[0] += 10.0
    # Privacy-masked password manager window
    ctx.update_window(
        ContextWindowChangedPayload(
            app_name="[隐私窗口已隐藏]",
            window_title="[隐私窗口已隐藏]",
            scene="privacy",
            privacy_masked=True,
        )
    )
    clock[0] += 10.0
    ctx.update_window(ContextWindowChangedPayload(app_name="Terminal", window_title="pytest -v", scene="coding"))
    clock[0] += 10.0

    ep = await extractor.persist_desktop_footprint(ctx)
    assert ep is not None
    assert "VS Code" in ep.summary
    assert "Terminal" in ep.summary
    assert "[隐私窗口已隐藏]" not in ep.summary
    assert "1Password" not in ep.summary

    all_eps = await store.list_episodes(status=None, limit=20)
    assert len(all_eps) == 1
    assert "[隐私窗口已隐藏]" not in all_eps[0].summary
    await store.close()


async def test_rolling_compression_summarizes_oldest_messages(tmp_path: Path) -> None:
    store, router, extractor = _make_extractor(tmp_path)
    await store.initialize()
    sess = await store.get_or_create_active_session()

    for i in range(6):
        await store.append_message(
            session_id=sess.session_id,
            turn_id=f"t{i}",
            role="user" if i % 2 == 0 else "assistant",
            content=f"消息 {i}：设定使用 FastAPI + SQLite WAL 模式。",
        )

    router.responses.append("早期对话已确定采用 FastAPI 与 SQLite WAL 架构。")
    did_compress = await extractor.maybe_compress_session(sess.session_id)
    assert did_compress is True

    updated_sess = await store.get_session(sess.session_id)
    assert updated_sess is not None
    assert "FastAPI" in updated_sess.rolling_summary
    assert updated_sess.last_summarized_msg_id == 4
    assert await store.count_unsummarized_messages(sess.session_id) == 2
    await store.close()


def test_memory_cli_commands_and_reindex(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "cli.db"

    async def _seed():
        s = SQLiteMemoryStore(db_path)
        await s.initialize()
        f = await s.add_fact(
            MemoryFact(
                fact_id="fact_cli_01",
                category="preference",
                content="回答技术问题直接给结论",
                importance=0.9,
            )
        )
        await s.close()
        return f.fact_id

    import asyncio
    fid = asyncio.run(_seed())

    assert cli_main(["--db", str(db_path), "list", "--type", "facts"]) == 0
    out = capsys.readouterr().out
    assert fid in out

    assert cli_main(["--db", str(db_path), "search", "技术问题"]) == 0
    out = capsys.readouterr().out
    assert fid in out

    assert cli_main(["--db", str(db_path), "inspect", fid]) == 0
    out = capsys.readouterr().out
    assert "回答技术问题直接给结论" in out

    snap = tmp_path / "cli_snap.jsonl"
    assert cli_main(["--db", str(db_path), "export", str(snap)]) == 0
    assert snap.exists()

    assert cli_main(["--db", str(db_path), "delete", fid]) == 0
    assert "Soft-deleted" in capsys.readouterr().out

    db2 = tmp_path / "cli_import.db"
    assert cli_main(["--db", str(db2), "import", str(snap)]) == 0
    assert cli_main(["--db", str(db2), "list", "--type", "facts"]) == 0
    assert fid in capsys.readouterr().out


def test_cli_reindex_with_mocked_embedding_endpoint(tmp_path: Path, monkeypatch, capsys) -> None:
    db_path = tmp_path / "reindex.db"
    cfg_path = tmp_path / "cfg_emb.yaml"
    cfg_path.write_text(
        """
default_profile: mock_local
profiles:
  mock_local:
    label: Mock
    provider: mock
    model: mock-v1
memory:
  backend: sqlite
  sqlite_path: """ + str(db_path) + """
  embedding:
    provider: openai_compatible
    base_url: http://127.0.0.1:11434/v1
    model: bge-m3
    timeout_ms: 800
""",
        encoding="utf-8",
    )

    async def _seed():
        s = SQLiteMemoryStore(db_path)
        await s.initialize()
        await s.add_fact(MemoryFact(fact_id="fact_ri_1", category="fact", content="显卡掉帧调试记录"))
        await s.close()

    import asyncio
    asyncio.run(_seed())

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"index": 0, "embedding": [0.5, 0.5, 0.0]}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            return FakeResp()

    import brain.memory.embeddings as emb_mod
    monkeypatch.setattr(emb_mod.httpx, "AsyncClient", FakeClient)

    assert cli_main(["--config", str(cfg_path), "reindex"]) == 0
    assert "Reindexed 1 active memory records" in capsys.readouterr().out

    assert cli_main(["--config", str(cfg_path), "inspect", "fact_ri_1"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["has_embedding"] is True
    assert inspected["embedding_model"] == "bge-m3"

