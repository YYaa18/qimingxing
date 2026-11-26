from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import List

from brain.memory.embeddings import (
    EmbeddingProvider,
    NullEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    pack_embedding,
)
from brain.memory.models import MemoryEpisode, MemoryFact, new_episode_id, new_fact_id
from brain.memory.retrieval import MemoryRetriever
from brain.memory.sqlite_store import SQLiteMemoryStore


async def test_5000_row_fts5_benchmark_under_10ms_p95(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "bench_5000.db")
    await store.initialize()

    # Seed 5,000 Chinese memory records directly in bulk transaction for fast test setup
    conn = store._ensure_conn()
    now = time.time()
    from brain.memory.models import tokenize_for_fts

    fact_rows = []
    fts_rows = []
    for i in range(4995):
        fid = f"fact_b_{i:05d}"
        content = f"日常记录第 {i} 条：调试微服务接口日志与异步队列监控指标 {i % 97}"
        fact_rows.append((fid, "fact", content, 0.5, 0.9, "active", "sess_b", now, now, now, None, None))
        fts_rows.append((fid, "fact", tokenize_for_fts(content)))

    target_id = "fact_target_gpu"
    target_content = "用户主力开发机是 Mac mini M4 配备 32GB 内存并运行本地量化模型"
    fact_rows.append((target_id, "tech_stack", target_content, 0.95, 0.99, "active", "sess_b", now, now, now, None, None))
    fts_rows.append((target_id, "fact", tokenize_for_fts(target_content)))

    conn.executemany(
        """
        INSERT INTO memory_facts (
            fact_id, category, content, importance, confidence, status,
            source_session_id, created_at, updated_at, last_accessed_at,
            embedding, embedding_model
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        fact_rows,
    )
    conn.executemany(
        "INSERT INTO memory_fts (item_id, item_type, search_tokens) VALUES (?, ?, ?)",
        fts_rows,
    )
    conn.commit()

    retriever = MemoryRetriever(store, NullEmbeddingProvider())

    latencies_ms: List[float] = []
    # Warm up once then measure 25 queries
    await retriever.search("Mac mini 内存配置是多少？", top_k=5)
    for _ in range(25):
        results, elapsed_ms = await retriever.search("我的 Mac mini 内存配置是什么", top_k=5)
        latencies_ms.append(elapsed_ms)
        assert any(r.item_id == target_id for r in results)

    latencies_ms.sort()
    p95 = latencies_ms[int(len(latencies_ms) * 0.95) - 1]
    assert p95 < 10.0, f"Expected FTS5 P95 < 10 ms on 5000 rows, got {p95:.2f} ms"
    await store.close()


class FakeSynonymEmbeddingProvider(EmbeddingProvider):
    """Deterministic embedding mock where '显卡掉帧' and '画面卡顿' share high cosine similarity."""

    def __init__(self, *, sleep_sec: float = 0.0) -> None:
        self.sleep_sec = sleep_sec

    @property
    def enabled(self) -> bool:
        return True

    @property
    def model_name(self) -> str:
        return "fake-bge-m3"

    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if self.sleep_sec > 0:
            await asyncio.sleep(self.sleep_sec)
        vecs: List[List[float]] = []
        for t in texts:
            if any(k in t for k in ("掉帧", "卡顿", "帧率")):
                vecs.append([0.95, 0.20, 0.05])
            else:
                vecs.append([0.05, 0.10, 0.98])
        return vecs


async def test_hybrid_rrf_synonym_recall_and_timeout_fallback(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "rrf.db")
    await store.initialize()

    # Store memory with wording "画面卡顿" (zero literal overlap with query "显卡掉帧")
    ep = await store.add_episode(
        MemoryEpisode(
            episode_id=new_episode_id(),
            kind="conversation",
            summary="昨天排查过游戏开启光追后画面卡顿的问题",
            importance=0.8,
        )
    )
    fact = await store.add_fact(
        MemoryFact(
            fact_id=new_fact_id(),
            category="preference",
            content="用户偏好深色界面主题",
            importance=0.7,
        )
    )

    emb = FakeSynonymEmbeddingProvider()
    vecs = await emb.embed_texts([ep.summary, fact.content])
    await store.update_episode_embedding(ep.episode_id, pack_embedding(vecs[0]), emb.model_name)
    await store.update_fact_embedding(fact.fact_id, pack_embedding(vecs[1]), emb.model_name)

    retriever = MemoryRetriever(store, emb, timeout_ms=800)
    results, _ = await retriever.search("为什么最近显卡掉帧这么严重？", top_k=3)
    assert len(results) >= 1
    assert results[0].item_id == ep.episode_id

    # Now test embedding timeout (> 100ms limit with 300ms sleep) -> must silently degrade to FTS5!
    slow_emb = FakeSynonymEmbeddingProvider(sleep_sec=0.35)
    slow_retriever = MemoryRetriever(store, slow_emb, timeout_ms=80)
    fts_only_results, elapsed_ms = await slow_retriever.search("画面卡顿", top_k=3)
    assert any(r.item_id == ep.episode_id for r in fts_only_results)
    assert elapsed_ms < 250.0  # bounded by timeout_ms, never hung

    await store.close()


async def test_openai_compatible_embedding_provider_http(monkeypatch) -> None:
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            assert url == "http://127.0.0.1:11434/v1/embeddings"
            assert kwargs["json"]["model"] == "bge-m3"
            return FakeResp()

    import brain.memory.embeddings as emb_mod

    monkeypatch.setattr(emb_mod.httpx, "AsyncClient", FakeClient)
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://127.0.0.1:11434/v1",
        model="bge-m3",
    )
    vecs = await provider.embed_texts(["测试文本"])
    assert vecs == [[0.1, 0.2, 0.3]]


async def test_embedding_provider_reuses_one_http_client(monkeypatch) -> None:
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"index": 0, "embedding": [0.5]}]}

    created: List["FakeClient"] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.closed = False
            created.append(self)

        async def post(self, url, **kwargs):
            return FakeResp()

        async def aclose(self):
            self.closed = True

    import brain.memory.embeddings as emb_mod

    monkeypatch.setattr(emb_mod.httpx, "AsyncClient", FakeClient)
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://127.0.0.1:11434/v1",
        model="bge-m3",
    )
    await provider.embed_texts(["a"])
    await provider.embed_texts(["b"])
    assert len(created) == 1
    await provider.aclose()
    assert created[0].closed
