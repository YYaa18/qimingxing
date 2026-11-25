from __future__ import annotations

import ast
import json
from pathlib import Path

from fastapi.testclient import TestClient

from brain.config import AppConfig, ProfileConfig
from brain.main import create_app
from brain.memory.models import MemoryEpisode, MemoryFact, new_episode_id, new_fact_id
from brain.memory.sqlite_store import SQLiteMemoryStore


def test_zero_sqlite3_imports_outside_sqlite_store() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for py_file in (repo_root / "brain").rglob("*.py"):
        if py_file.name in ("sqlite_store.py", "model_config_store.py"):
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "sqlite3":
                        offenders.append(str(py_file))
            elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
                offenders.append(str(py_file))
    assert offenders == [], f"Forbidden sqlite3 import in: {offenders}"


async def test_sqlite_schema_wal_slots_and_lossless_jsonl_roundtrip(tmp_path: Path) -> None:
    db1_path = tmp_path / "mem1.db"
    store1 = SQLiteMemoryStore(db1_path)
    await store1.initialize()

    # Check WAL mode and nullable embedding / embedding_model columns
    conn = store1._ensure_conn()
    mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert str(mode).lower() == "wal"
    fact_cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_facts);").fetchall()}
    ep_cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_episodes);").fetchall()}
    assert {"embedding", "embedding_model"}.issubset(fact_cols)
    assert {"embedding", "embedding_model"}.issubset(ep_cols)

    sess = await store1.get_or_create_active_session()
    await store1.append_message(
        session_id=sess.session_id,
        turn_id="t1",
        role="user",
        content="我正在开发 qimingxing 记忆模块",
        desktop_context_summary="VS Code — sqlite_store.py",
    )
    await store1.update_session_summary(sess.session_id, "讨论了 SQLite 记忆架构。", 1)

    fact = await store1.add_fact(
        MemoryFact(
            fact_id=new_fact_id(),
            category="project",
            content="正在开发启明星长期记忆底座",
            importance=0.9,
            source_session_id=sess.session_id,
            embedding=b"\x00\x00\x80?",
            embedding_model="bge-m3",
        )
    )
    ep = await store1.add_episode(
        MemoryEpisode(
            episode_id=new_episode_id(),
            kind="desktop_footprint",
            summary="桌面工作足迹：在 VS Code 编写记忆存储测试",
            keywords="VS Code",
        )
    )

    snapshot_path = tmp_path / "snapshot.jsonl"
    exported_count = await store1.export_snapshot(snapshot_path)
    assert exported_count >= 11
    await store1.close()

    # Import into a brand new empty DB
    db2_path = tmp_path / "mem2.db"
    store2 = SQLiteMemoryStore(db2_path)
    await store2.initialize()
    imported_count = await store2.import_snapshot(snapshot_path)
    assert imported_count == exported_count

    restored_sess = await store2.get_session(sess.session_id)
    assert restored_sess is not None
    assert restored_sess.rolling_summary == "讨论了 SQLite 记忆架构。"

    restored_fact = await store2.get_fact(fact.fact_id)
    assert restored_fact is not None
    assert restored_fact.content == "正在开发启明星长期记忆底座"
    assert restored_fact.embedding == b"\x00\x00\x80?"
    assert restored_fact.embedding_model == "bge-m3"

    restored_ep = await store2.get_episode(ep.episode_id)
    assert restored_ep is not None
    assert restored_ep.kind == "desktop_footprint"

    # FTS index rebuilt cleanly on import
    from brain.memory.models import tokenize_query_for_fts
    fts_hits = await store2.fts_search(tokenize_query_for_fts("启明星"))
    assert any(item_id == fact.fact_id for item_id, _t, _s in fts_hits)
    await store2.close()


def test_cross_restart_continuity_and_clear_history_preserves_long_term_facts(tmp_path: Path) -> None:
    db_file = tmp_path / "restart_memory.db"

    def _build_app():
        cfg = AppConfig(
            default_profile="mock_local",
            profiles={
                "mock_local": ProfileConfig(
                    name="mock_local", label="Mock", provider="mock", model="mock-v1"
                )
            },
        )
        cfg.memory.sqlite_path = str(db_file)
        cfg.memory.extraction.enabled = False
        return create_app(config=cfg)

    # First Brain lifecycle
    app1 = _build_app()
    with TestClient(app1) as client1:
        with client1.websocket_connect("/ws") as ws1:
            ws1.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "h1",
                        "ts": 1,
                        "source": "body",
                        "type": "system.hello",
                        "payload": {"client_name": "body", "role": "body"},
                    }
                )
            )
            ws1.receive_text()
            ws1.receive_text()

            ws1.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "m1",
                        "turn_id": "t_1",
                        "ts": 2,
                        "source": "body",
                        "type": "input.text",
                        "payload": {"text": "记住我叫阿星"},
                    }
                )
            )
            while True:
                if json.loads(ws1.receive_text())["type"] == "output.turn_end":
                    break
            ws1.receive_text()

        # Pre-seed an L2 fact as if extracted
        import asyncio
        asyncio.run(
            app1.state.memory_backend.add_fact(
                MemoryFact(fact_id=new_fact_id(), category="fact", content="用户名字叫阿星", importance=0.95)
            )
        )

    # Second Brain lifecycle (simulating full process restart)
    app2 = _build_app()
    with TestClient(app2) as client2:
        dbg = client2.get("/api/memory/debug").json()
        assert dbg["active_session"]["recent_message_count"] >= 2
        assert dbg["active_facts_count"] == 1

        with client2.websocket_connect("/ws") as ws2:
            ws2.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "h2",
                        "ts": 3,
                        "source": "body",
                        "type": "system.hello",
                        "payload": {"client_name": "body", "role": "body"},
                    }
                )
            )
            ws2.receive_text()
            ws2.receive_text()

            # clear_history archives old session & opens new session, keeping L2 facts intact
            ws2.send_text(
                json.dumps(
                    {
                        "v": "1.0",
                        "id": "clr",
                        "ts": 4,
                        "source": "body",
                        "type": "input.action",
                        "payload": {"action": "clear_history"},
                    }
                )
            )
            assert json.loads(ws2.receive_text())["type"] == "output.tool_status"

        dbg_after = client2.get("/api/memory/debug").json()
        assert dbg_after["active_session"]["session_id"] != dbg["active_session"]["session_id"]
        assert dbg_after["active_session"]["recent_message_count"] == 0
        assert dbg_after["active_facts_count"] == 1
