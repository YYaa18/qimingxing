from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from brain.memory.models import (
    BackgroundTaskRecord,
    FeedbackRecord,
    MemoryCategory,
    MemoryEpisode,
    MemoryFact,
    MemorySkill,
    ResourceLockRecord,
    ScheduledTaskRecord,
    SessionRecord,
    SkillStatus,
    FactStatus,
    EpisodeStatus,
    TaskInboxRecord,
    UsageLedgerRecord,
    UsageSlotSummary,
    UsageSummary,
    WorkHistoryRecord,
)
from brain.memory.models import tokenize_for_fts
from brain.memory.rows import (
    _parse_json_dict,
    _parse_json_list,
    _row_to_background_task,
    _row_to_category,
    _row_to_episode,
    _row_to_fact,
    _row_to_message,
    _row_to_scheduled_task,
    _row_to_session,
    _row_to_skill,
    _row_to_work_history,
    _snapshot_rich,
)


class FactsMixin:
    """Moved verbatim from SQLiteMemoryStore."""

    async def register_category(self, category: MemoryCategory) -> MemoryCategory:
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                """
                INSERT INTO memory_categories (
                    category_key, display_name, description, tier,
                    retrieval_weight, decay_rate, inject_to_l1, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(category_key) DO UPDATE SET
                    display_name = excluded.display_name,
                    description = excluded.description,
                    tier = excluded.tier,
                    retrieval_weight = excluded.retrieval_weight,
                    decay_rate = excluded.decay_rate,
                    inject_to_l1 = excluded.inject_to_l1
                """,
                (
                    category.category_key,
                    category.display_name,
                    category.description,
                    category.tier,
                    category.retrieval_weight,
                    category.decay_rate,
                    1 if category.inject_to_l1 else 0,
                    category.created_by,
                    category.created_at,
                ),
            )
            conn.commit()
            return category

    async def get_category(self, category_key: str) -> Optional[MemoryCategory]:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT * FROM memory_categories WHERE category_key = ?", (category_key,)
            ).fetchone()
            return _row_to_category(row) if row else None

    async def list_categories(self) -> List[MemoryCategory]:
        async with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(
                "SELECT * FROM memory_categories ORDER BY retrieval_weight DESC, created_at ASC"
            ).fetchall()
            return [_row_to_category(r) for r in rows]

    # --- L2: Facts ---

    def _sync_fts_entry(
        self,
        conn: sqlite3.Connection,
        item_id: str,
        item_type: str,
        text: Optional[str],
    ) -> None:
        conn.execute(
            "DELETE FROM memory_fts WHERE item_id = ? AND item_type = ?",
            (item_id, item_type),
        )
        if text:
            tokens = tokenize_for_fts(text)
            if tokens:
                conn.execute(
                    "INSERT INTO memory_fts (item_id, item_type, search_tokens) VALUES (?, ?, ?)",
                    (item_id, item_type, tokens),
                )

    async def add_fact(self, fact: MemoryFact) -> MemoryFact:
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                """
                INSERT INTO memory_facts (
                    fact_id, category, content, importance, confidence, status,
                    source_session_id, created_at, updated_at, last_accessed_at,
                    embedding, embedding_model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fact.fact_id,
                    fact.category,
                    fact.content,
                    fact.importance,
                    fact.confidence,
                    fact.status,
                    fact.source_session_id,
                    fact.created_at,
                    fact.updated_at,
                    fact.last_accessed_at,
                    fact.embedding,
                    fact.embedding_model,
                ),
            )
            if fact.status == "active":
                self._sync_fts_entry(conn, fact.fact_id, "fact", f"{fact.category} {fact.content}")
            conn.commit()
            return fact

    async def get_fact(self, fact_id: str) -> Optional[MemoryFact]:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT * FROM memory_facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            return _row_to_fact(row) if row else None

    async def get_facts_by_ids(self, fact_ids: List[str]) -> Dict[str, MemoryFact]:
        ids = sorted({str(fid) for fid in fact_ids if str(fid)})
        if not ids:
            return {}
        async with self._lock:
            conn = self._ensure_conn()
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT * FROM memory_facts WHERE fact_id IN ({placeholders})",
                tuple(ids),
            ).fetchall()
            return {str(row["fact_id"]): _row_to_fact(row) for row in rows}

    async def list_facts(
        self,
        *,
        status: Optional[FactStatus] = "active",
        limit: int = 100,
    ) -> List[MemoryFact]:
        async with self._lock:
            conn = self._ensure_conn()
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM memory_facts ORDER BY importance DESC, updated_at DESC, fact_id ASC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM memory_facts
                    WHERE status = ?
                    ORDER BY importance DESC, updated_at DESC, fact_id ASC
                    LIMIT ?
                    """,
                    (status, limit),
                ).fetchall()
            return [_row_to_fact(r) for r in rows]

    async def update_fact_status(self, fact_id: str, status: FactStatus) -> bool:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT category, content FROM memory_facts WHERE fact_id = ?",
                (fact_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE memory_facts SET status = ?, updated_at = ? WHERE fact_id = ?",
                (status, time.time(), fact_id),
            )
            if status == "active":
                self._sync_fts_entry(conn, fact_id, "fact", f"{row['category']} {row['content']}")
            else:
                self._sync_fts_entry(conn, fact_id, "fact", None)
            conn.commit()
            return True

    async def touch_fact(self, fact_id: str) -> None:
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                "UPDATE memory_facts SET last_accessed_at = ? WHERE fact_id = ?",
                (time.time(), fact_id),
            )
            conn.commit()

    async def update_fact_embedding(
        self,
        fact_id: str,
        embedding: bytes,
        embedding_model: str,
    ) -> None:
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                "UPDATE memory_facts SET embedding = ?, embedding_model = ? WHERE fact_id = ?",
                (embedding, embedding_model, fact_id),
            )
            conn.commit()

    # --- L3: Episodes ---

    async def add_episode(self, episode: MemoryEpisode) -> MemoryEpisode:
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                """
                INSERT INTO memory_episodes (
                    episode_id, kind, summary, keywords, importance,
                    time_start, time_end, status, created_at,
                    embedding, embedding_model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode.episode_id,
                    episode.kind,
                    episode.summary,
                    episode.keywords,
                    episode.importance,
                    episode.time_start,
                    episode.time_end,
                    episode.status,
                    episode.created_at,
                    episode.embedding,
                    episode.embedding_model,
                ),
            )
            if episode.status == "active":
                self._sync_fts_entry(
                    conn, episode.episode_id, "episode", f"{episode.keywords} {episode.summary}"
                )
            conn.commit()
            return episode

    async def get_episode(self, episode_id: str) -> Optional[MemoryEpisode]:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT * FROM memory_episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
            return _row_to_episode(row) if row else None

    async def get_episodes_by_ids(
        self, episode_ids: List[str]
    ) -> Dict[str, MemoryEpisode]:
        ids = sorted({str(eid) for eid in episode_ids if str(eid)})
        if not ids:
            return {}
        async with self._lock:
            conn = self._ensure_conn()
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT * FROM memory_episodes WHERE episode_id IN ({placeholders})",
                tuple(ids),
            ).fetchall()
            return {str(row["episode_id"]): _row_to_episode(row) for row in rows}

    async def list_episodes(
        self,
        *,
        status: Optional[EpisodeStatus] = "active",
        kind: Optional[EpisodeKind] = None,
        limit: int = 100,
    ) -> List[MemoryEpisode]:
        async with self._lock:
            conn = self._ensure_conn()
            clauses: list[str] = []
            params: list[object] = []
            if status is not None:
                clauses.append("status = ?")
                params.append(status)
            if kind is not None:
                clauses.append("kind = ?")
                params.append(kind)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            params.append(limit)
            rows = conn.execute(
                f"SELECT * FROM memory_episodes {where} ORDER BY time_end DESC LIMIT ?",
                tuple(params),
            ).fetchall()
            return [_row_to_episode(r) for r in rows]

    async def update_episode_status(self, episode_id: str, status: EpisodeStatus) -> bool:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT keywords, summary FROM memory_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE memory_episodes SET status = ? WHERE episode_id = ?",
                (status, episode_id),
            )
            if status == "active":
                self._sync_fts_entry(
                    conn, episode_id, "episode", f"{row['keywords']} {row['summary']}"
                )
            else:
                self._sync_fts_entry(conn, episode_id, "episode", None)
            conn.commit()
            return True

    async def update_episode_embedding(
        self,
        episode_id: str,
        embedding: bytes,
        embedding_model: str,
    ) -> None:
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                "UPDATE memory_episodes SET embedding = ?, embedding_model = ? WHERE episode_id = ?",
                (embedding, embedding_model, episode_id),
            )
            conn.commit()

    # --- Search ---

    async def fts_search(
        self,
        query_expr: str,
        *,
        limit: int = 40,
    ) -> List[Tuple[str, str, float]]:
        if not query_expr.strip():
            return []
        async with self._lock:
            conn = self._ensure_conn()
            try:
                rows = conn.execute(
                    """
                    SELECT item_id, item_type, bm25(memory_fts) AS rank
                    FROM memory_fts
                    WHERE memory_fts MATCH ? AND item_type IN ('fact', 'episode', 'message')
                    ORDER BY rank ASC
                    LIMIT ?
                    """,
                    (query_expr, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return []

            if not rows:
                return []
            # SQLite bm25() returns negative values where more negative = better match
            raw_scores = [max(0.0, -float(r["rank"])) for r in rows]
            max_score = max(raw_scores) or 1.0
            return [
                (str(r["item_id"]), str(r["item_type"]), s / max_score)
                for r, s in zip(rows, raw_scores)
            ]

    async def list_embedded_items(
        self,
        embedding_model: str,
    ) -> List[Tuple[str, str, bytes]]:
        async with self._lock:
            conn = self._ensure_conn()
            facts = conn.execute(
                """
                SELECT fact_id, embedding FROM memory_facts
                WHERE status = 'active' AND embedding IS NOT NULL AND embedding_model = ?
                """,
                (embedding_model,),
            ).fetchall()
            episodes = conn.execute(
                """
                SELECT episode_id, embedding FROM memory_episodes
                WHERE status = 'active' AND embedding IS NOT NULL AND embedding_model = ?
                """,
                (embedding_model,),
            ).fetchall()
            items: List[Tuple[str, str, bytes]] = []
            for r in facts:
                items.append((str(r["fact_id"]), "fact", bytes(r["embedding"])))
            for r in episodes:
                items.append((str(r["episode_id"]), "episode", bytes(r["embedding"])))
            return items

    # --- M4: Procedural Skills, Work History & Usage Ledger ---

