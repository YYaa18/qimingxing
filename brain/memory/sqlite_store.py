from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from brain.memory.backend import SNAPSHOT_SCHEMA_VERSION, MemoryBackend
from brain.memory.models import (
    DEFAULT_MEMORY_CATEGORIES,
    BackgroundTaskRecord,
    BackgroundTaskStatus,
    ResourceLockRecord,
    EpisodeKind,
    EpisodeStatus,
    FactStatus,
    FeedbackRecord,
    MemoryCategory,
    MemoryEpisode,
    MemoryFact,
    MemorySkill,
    MessageRecord,
    ScheduleKind,
    ScheduledTaskRecord,
    SessionRecord,
    SkillStatus,
    TaskInboxRecord,
    TaskStatus,
    TaskType,
    UsageLedgerRecord,
    UsageSlotSummary,
    UsageSummary,
    WorkHistoryRecord,
    new_session_id,
    tokenize_for_fts,
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    rolling_summary TEXT NOT NULL DEFAULT '',
    last_summarized_msg_id INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS messages (
    msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    emotion TEXT NOT NULL DEFAULT 'neutral',
    desktop_context_summary TEXT NOT NULL DEFAULT '',
    sources_json TEXT NOT NULL DEFAULT '[]',
    media_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session_msg
    ON messages(session_id, msg_id);

CREATE TABLE IF NOT EXISTS message_feedback (
    feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    rating TEXT NOT NULL,
    profile TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    answer TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_feedback_turn
    ON message_feedback(turn_id);

CREATE INDEX IF NOT EXISTS idx_feedback_created
    ON message_feedback(created_at DESC);

CREATE TABLE IF NOT EXISTS memory_facts (
    fact_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    content TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.7,
    confidence REAL NOT NULL DEFAULT 0.9,
    status TEXT NOT NULL DEFAULT 'active',
    source_session_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_accessed_at REAL NOT NULL,
    embedding BLOB NULL,
    embedding_model TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_facts_status_imp
    ON memory_facts(status, importance DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS memory_episodes (
    episode_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    importance REAL NOT NULL DEFAULT 0.6,
    time_start REAL NOT NULL,
    time_end REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    embedding BLOB NULL,
    embedding_model TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_episodes_status_time
    ON memory_episodes(status, time_end DESC);

CREATE TABLE IF NOT EXISTS memory_categories (
    category_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL DEFAULT 'episodic',
    retrieval_weight REAL NOT NULL DEFAULT 0.85,
    decay_rate REAL NOT NULL DEFAULT 0.05,
    inject_to_l1 INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT 'system',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages_archive (
    msg_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    emotion TEXT NOT NULL DEFAULT 'neutral',
    desktop_context_summary TEXT NOT NULL DEFAULT '',
    sources_json TEXT NOT NULL DEFAULT '[]',
    media_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    archived_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_archive_session
    ON messages_archive(session_id, msg_id);

CREATE TABLE IF NOT EXISTS memory_skills (
    skill_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    trigger_intent TEXT NOT NULL,
    tool_preference TEXT NOT NULL DEFAULT 'shell',
    prerequisites TEXT NOT NULL DEFAULT '',
    steps_json TEXT NOT NULL DEFAULT '[]',
    success_count INTEGER NOT NULL DEFAULT 1,
    failure_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skills_status_score
    ON memory_skills(status, success_count DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS work_history (
    work_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL,
    result_summary TEXT NOT NULL,
    trace_json TEXT NOT NULL DEFAULT '[]',
    step_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'completed',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_work_history_created
    ON work_history(created_at DESC);

CREATE TABLE IF NOT EXISTS usage_ledger (
    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    profile TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    slot TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    prompt_cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_cny REAL NOT NULL DEFAULT 0.0,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_created
    ON usage_ledger(created_at DESC);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    task_type TEXT NOT NULL DEFAULT 'reminder',
    schedule_kind TEXT NOT NULL DEFAULT 'once',
    schedule_expr TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    next_run_at REAL NOT NULL,
    last_run_at REAL NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scheduled_due
    ON scheduled_tasks(status, next_run_at ASC);

CREATE TABLE IF NOT EXISTS background_tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal_prompt TEXT NOT NULL,
    source_session_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    progress_pct INTEGER NOT NULL DEFAULT 0,
    current_step TEXT NOT NULL DEFAULT '',
    result_summary TEXT NOT NULL DEFAULT '',
    artifact_path TEXT NOT NULL DEFAULT '',
    bundle_path TEXT NOT NULL DEFAULT '',
    result_files_json TEXT NOT NULL DEFAULT '[]',
    error_message TEXT NOT NULL DEFAULT '',
    slot_used TEXT NOT NULL DEFAULT 'bg_task',
    worker_session_id TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'explicit_user',
    mandate_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    result_refs_json TEXT NOT NULL DEFAULT '[]',
    watchdog_state_json TEXT NOT NULL DEFAULT '{}',
    last_heartbeat_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL NULL,
    finished_at REAL NULL
);

CREATE TABLE IF NOT EXISTS task_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    consumed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_task_inbox_pending
    ON task_inbox(task_id, consumed_at);

CREATE TABLE IF NOT EXISTS resource_locks (
    resource_key TEXT NOT NULL,
    task_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    lease_expires_at REAL NOT NULL,
    PRIMARY KEY (resource_key, task_id)
);

CREATE INDEX IF NOT EXISTS idx_resource_locks_expiry
    ON resource_locks(lease_expires_at);

CREATE INDEX IF NOT EXISTS idx_bg_tasks_status_created
    ON background_tasks(status, created_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    item_id UNINDEXED,
    item_type UNINDEXED,
    search_tokens,
    tokenize='unicode61'
);
"""


class SQLiteMemoryStore(MemoryBackend):
    """Default zero-ops single-file SQLite WAL + FTS5 3-layer memory store."""

    def __init__(self, db_path: Path | str = "data/qimingxing_memory.db") -> None:
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.executescript(_SCHEMA_SQL)
            usage_cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(usage_ledger)").fetchall()
            }
            if "provider" not in usage_cols:
                conn.execute(
                    "ALTER TABLE usage_ledger ADD COLUMN provider TEXT NOT NULL DEFAULT ''"
                )
            if "slot" not in usage_cols:
                conn.execute(
                    "ALTER TABLE usage_ledger ADD COLUMN slot TEXT NOT NULL DEFAULT ''"
                )
            if "cached_input_tokens" not in usage_cols:
                conn.execute(
                    "ALTER TABLE usage_ledger ADD COLUMN cached_input_tokens INTEGER NOT NULL DEFAULT 0"
                )
            # Reconcile on every startup: older app versions may still write the
            # legacy column after the one-time migration.
            conn.execute(
                """UPDATE usage_ledger SET cached_input_tokens = prompt_cache_hit_tokens
                   WHERE cached_input_tokens = 0 AND prompt_cache_hit_tokens > 0"""
            )
            for table in ("messages", "messages_archive"):
                msg_cols = {
                    str(r["name"])
                    for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if "sources_json" not in msg_cols:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN sources_json TEXT NOT NULL DEFAULT '[]'"
                    )
                if "media_json" not in msg_cols:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN media_json TEXT NOT NULL DEFAULT '[]'"
                    )
            bg_cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(background_tasks)").fetchall()
            }
            bg_additions = {
                "worker_session_id": "TEXT NOT NULL DEFAULT ''",
                "origin": "TEXT NOT NULL DEFAULT 'explicit_user'",
                "mandate_json": "TEXT NOT NULL DEFAULT '{}'",
                "checkpoint_json": "TEXT NOT NULL DEFAULT '{}'",
                "result_refs_json": "TEXT NOT NULL DEFAULT '[]'",
                "watchdog_state_json": "TEXT NOT NULL DEFAULT '{}'",
                "last_heartbeat_at": "REAL NOT NULL DEFAULT 0",
                "bundle_path": "TEXT NOT NULL DEFAULT ''",
                "result_files_json": "TEXT NOT NULL DEFAULT '[]'",
            }
            for col, decl in bg_additions.items():
                if col not in bg_cols:
                    conn.execute(f"ALTER TABLE background_tasks ADD COLUMN {col} {decl}")
            for cat in DEFAULT_MEMORY_CATEGORIES:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO memory_categories (
                        category_key, display_name, description, tier,
                        retrieval_weight, decay_rate, inject_to_l1, created_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cat.category_key,
                        cat.display_name,
                        cat.description,
                        cat.tier,
                        cat.retrieval_weight,
                        cat.decay_rate,
                        1 if cat.inject_to_l1 else 0,
                        cat.created_by,
                        cat.created_at,
                    ),
                )
            conn.commit()
            self._conn = conn
        return self._conn

    async def initialize(self) -> None:
        async with self._lock:
            self._ensure_conn()

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # --- L1: Sessions & Messages ---

    async def get_or_create_active_session(self) -> SessionRecord:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT * FROM sessions WHERE status = 'active' ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            if row is not None:
                return _row_to_session(row)

            now = time.time()
            rec = SessionRecord(
                session_id=new_session_id(),
                started_at=now,
                updated_at=now,
            )
            conn.execute(
                """
                INSERT INTO sessions (session_id, started_at, updated_at, rolling_summary, last_summarized_msg_id, status)
                VALUES (?, ?, ?, '', 0, 'active')
                """,
                (rec.session_id, rec.started_at, rec.updated_at),
            )
            conn.commit()
            return rec

    async def create_new_session(self, *, archive_previous: bool = True) -> SessionRecord:
        async with self._lock:
            conn = self._ensure_conn()
            now = time.time()
            if archive_previous:
                conn.execute(
                    "UPDATE sessions SET status = 'archived', updated_at = ? WHERE status = 'active'",
                    (now,),
                )
            rec = SessionRecord(
                session_id=new_session_id(),
                started_at=now,
                updated_at=now,
            )
            conn.execute(
                """
                INSERT INTO sessions (session_id, started_at, updated_at, rolling_summary, last_summarized_msg_id, status)
                VALUES (?, ?, ?, '', 0, 'active')
                """,
                (rec.session_id, rec.started_at, rec.updated_at),
            )
            conn.commit()
            return rec

    async def get_session(self, session_id: str) -> Optional[SessionRecord]:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return _row_to_session(row) if row else None

    async def list_sessions(self, *, limit: int = 20) -> List[SessionRecord]:
        async with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [_row_to_session(r) for r in rows]

    async def update_session_summary(
        self,
        session_id: str,
        rolling_summary: str,
        last_summarized_msg_id: int,
    ) -> None:
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                """
                UPDATE sessions
                SET rolling_summary = ?, last_summarized_msg_id = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (rolling_summary, last_summarized_msg_id, time.time(), session_id),
            )
            conn.commit()

    async def append_message(
        self,
        *,
        session_id: str,
        turn_id: str,
        role: str,
        content: str,
        emotion: str = "neutral",
        desktop_context_summary: str = "",
        sources: Optional[List[Dict[str, Any]]] = None,
        media: Optional[List[Dict[str, Any]]] = None,
    ) -> MessageRecord:
        async with self._lock:
            conn = self._ensure_conn()
            now = time.time()
            sources = sources or []
            media = media or []
            cur = conn.execute(
                """
                INSERT INTO messages (
                    session_id, turn_id, role, content, emotion,
                    desktop_context_summary, sources_json, media_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, turn_id, role, content, emotion,
                    desktop_context_summary,
                    json.dumps(sources, ensure_ascii=False),
                    json.dumps(media, ensure_ascii=False),
                    now,
                ),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            msg_id = int(cur.lastrowid)
            if len(content.strip()) >= 6:
                self._sync_fts_entry(conn, str(msg_id), "message", content)
            conn.commit()
            return MessageRecord(
                msg_id=msg_id,
                session_id=session_id,
                turn_id=turn_id,
                role=role,
                content=content,
                emotion=emotion,
                desktop_context_summary=desktop_context_summary,
                sources=sources,
                media=media,
                created_at=now,
            )

    async def get_messages(
        self,
        session_id: str,
        *,
        after_msg_id: int = 0,
        limit: int = 50,
        oldest_first: bool = False,
    ) -> List[MessageRecord]:
        async with self._lock:
            conn = self._ensure_conn()
            if oldest_first:
                rows = conn.execute(
                    """
                    SELECT * FROM messages
                    WHERE session_id = ? AND msg_id > ?
                    ORDER BY msg_id ASC
                    LIMIT ?
                    """,
                    (session_id, after_msg_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM messages
                        WHERE session_id = ? AND msg_id > ?
                        ORDER BY msg_id DESC
                        LIMIT ?
                    ) sub ORDER BY msg_id ASC
                    """,
                    (session_id, after_msg_id, limit),
                ).fetchall()
            return [_row_to_message(r) for r in rows]

    async def count_unsummarized_messages(self, session_id: str) -> int:
        async with self._lock:
            conn = self._ensure_conn()
            sess_row = conn.execute(
                "SELECT last_summarized_msg_id FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            watermark = int(sess_row["last_summarized_msg_id"]) if sess_row else 0
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM messages WHERE session_id = ? AND msg_id > ?",
                (session_id, watermark),
            ).fetchone()
            return int(row["cnt"]) if row else 0

    async def get_message(self, msg_id: int) -> Optional[MessageRecord]:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT * FROM messages WHERE msg_id = ?", (msg_id,)
            ).fetchone()
            return _row_to_message(row) if row else None

    async def get_messages_by_ids(
        self, msg_ids: List[int]
    ) -> Dict[int, MessageRecord]:
        ids = sorted({int(mid) for mid in msg_ids})
        if not ids:
            return {}
        async with self._lock:
            conn = self._ensure_conn()
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT * FROM messages WHERE msg_id IN ({placeholders})",
                tuple(ids),
            ).fetchall()
            return {int(row["msg_id"]): _row_to_message(row) for row in rows}

    async def count_messages(self) -> Tuple[int, int]:
        async with self._lock:
            conn = self._ensure_conn()
            hot_cnt = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            cold_cnt = int(conn.execute("SELECT COUNT(*) FROM messages_archive").fetchone()[0])
            return hot_cnt, cold_cnt

    async def archive_old_messages(
        self,
        *,
        keep_latest: int = 10000,
        batch_size: int = 2000,
    ) -> int:
        async with self._lock:
            conn = self._ensure_conn()
            hot_cnt = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            excess = hot_cnt - max(0, keep_latest)
            if excess <= 0:
                return 0
            to_move = min(excess, max(1, batch_size))
            # Never archive unsummarized messages in the active session; move archived sessions first
            rows = conn.execute(
                """
                SELECT m.* FROM messages m
                LEFT JOIN sessions s ON m.session_id = s.session_id
                WHERE s.status = 'archived'
                   OR s.status IS NULL
                   OR (s.status = 'active' AND m.msg_id <= COALESCE(s.last_summarized_msg_id, 0))
                ORDER BY CASE WHEN s.status = 'archived' THEN 0 ELSE 1 END ASC, m.msg_id ASC
                LIMIT ?
                """,
                (to_move,),
            ).fetchall()
            if not rows:
                return 0
            now = time.time()
            msg_ids = [int(r["msg_id"]) for r in rows]
            conn.executemany(
                """
                INSERT OR REPLACE INTO messages_archive (
                    msg_id, session_id, turn_id, role, content,
                    emotion, desktop_context_summary, sources_json, media_json,
                    created_at, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        r["msg_id"],
                        r["session_id"],
                        r["turn_id"],
                        r["role"],
                        r["content"],
                        r["emotion"],
                        r["desktop_context_summary"],
                        r["sources_json"] if "sources_json" in r.keys() else "[]",
                        r["media_json"] if "media_json" in r.keys() else "[]",
                        r["created_at"],
                        now,
                    )
                    for r in rows
                ],
            )
            placeholders = ",".join("?" for _ in msg_ids)
            conn.execute(f"DELETE FROM messages WHERE msg_id IN ({placeholders})", msg_ids)
            conn.executemany(
                "DELETE FROM memory_fts WHERE item_id = ? AND item_type = 'message'",
                [(str(mid),) for mid in msg_ids],
            )
            conn.commit()
            return len(msg_ids)

    # --- Dynamic Category Registry ---

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

    async def upsert_skill(self, skill: MemorySkill) -> MemorySkill:
        async with self._lock:
            conn = self._ensure_conn()
            now = skill.updated_at or time.time()
            conn.execute(
                """
                INSERT INTO memory_skills (
                    skill_id, name, trigger_intent, tool_preference, prerequisites,
                    steps_json, success_count, failure_count, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(skill_id) DO UPDATE SET
                    name = excluded.name,
                    trigger_intent = excluded.trigger_intent,
                    tool_preference = excluded.tool_preference,
                    prerequisites = excluded.prerequisites,
                    steps_json = excluded.steps_json,
                    success_count = excluded.success_count,
                    failure_count = excluded.failure_count,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    skill.skill_id,
                    skill.name,
                    skill.trigger_intent,
                    skill.tool_preference,
                    skill.prerequisites,
                    skill.steps_json,
                    skill.success_count,
                    skill.failure_count,
                    skill.status,
                    now,
                ),
            )
            if skill.status == "active":
                fts_text = f"{skill.name} {skill.trigger_intent} {skill.prerequisites} {skill.steps_json}"
                self._sync_fts_entry(conn, skill.skill_id, "skill", fts_text)
            else:
                self._sync_fts_entry(conn, skill.skill_id, "skill", None)
            conn.commit()
            return skill

    async def get_skill(self, skill_id: str) -> Optional[MemorySkill]:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT * FROM memory_skills WHERE skill_id = ?", (skill_id,)
            ).fetchone()
            return _row_to_skill(row) if row else None

    async def list_skills(
        self,
        *,
        status: Optional[SkillStatus] = "active",
        limit: int = 100,
    ) -> List[MemorySkill]:
        async with self._lock:
            conn = self._ensure_conn()
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM memory_skills ORDER BY success_count DESC, updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memory_skills WHERE status = ? ORDER BY success_count DESC, updated_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            return [_row_to_skill(r) for r in rows]

    async def search_skills(self, query_expr: str, *, limit: int = 5) -> List[MemorySkill]:
        if not query_expr or not query_expr.strip():
            return []
        async with self._lock:
            conn = self._ensure_conn()
            try:
                fts_rows = conn.execute(
                    """
                    SELECT item_id, bm25(memory_fts) AS rank
                    FROM memory_fts
                    WHERE memory_fts MATCH ? AND item_type = 'skill'
                    ORDER BY rank ASC
                    LIMIT ?
                    """,
                    (query_expr, limit * 3),
                ).fetchall()
            except sqlite3.OperationalError:
                return []

            results: List[Tuple[float, MemorySkill]] = []
            for r in fts_rows:
                srow = conn.execute(
                    "SELECT * FROM memory_skills WHERE skill_id = ? AND status = 'active'",
                    (str(r["item_id"]),),
                ).fetchone()
                if srow is None:
                    continue
                skill = _row_to_skill(srow)
                bm25_score = max(0.0, -float(r["rank"]))
                reliability = (skill.success_count + 1) / (
                    skill.success_count + skill.failure_count + 2
                )
                results.append((bm25_score * (0.6 + 0.4 * reliability), skill))
            results.sort(key=lambda pair: pair[0], reverse=True)
            return [s for _, s in results[:limit]]

    async def record_skill_outcome(
        self, skill_id: str, *, success: bool
    ) -> Optional[MemorySkill]:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT * FROM memory_skills WHERE skill_id = ?", (skill_id,)
            ).fetchone()
            if row is None:
                return None
            succ = int(row["success_count"]) + (1 if success else 0)
            fail = int(row["failure_count"]) + (0 if success else 1)
            new_status: SkillStatus = str(row["status"])  # type: ignore[assignment]
            if not success and fail >= 3 and fail > succ:
                new_status = "deprecated"
            elif success:
                new_status = "active"
            now = time.time()
            conn.execute(
                """
                UPDATE memory_skills
                SET success_count = ?, failure_count = ?, status = ?, updated_at = ?
                WHERE skill_id = ?
                """,
                (succ, fail, new_status, now, skill_id),
            )
            if new_status != "active":
                self._sync_fts_entry(conn, skill_id, "skill", None)
            else:
                fts_text = (
                    f"{row['name']} {row['trigger_intent']} "
                    f"{row['prerequisites']} {row['steps_json']}"
                )
                self._sync_fts_entry(conn, skill_id, "skill", fts_text)
            conn.commit()
            updated_row = conn.execute(
                "SELECT * FROM memory_skills WHERE skill_id = ?", (skill_id,)
            ).fetchone()
            return _row_to_skill(updated_row) if updated_row else None

    async def update_skill_status(self, skill_id: str, status: SkillStatus) -> bool:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT * FROM memory_skills WHERE skill_id = ?", (skill_id,)
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE memory_skills SET status = ?, updated_at = ? WHERE skill_id = ?",
                (status, time.time(), skill_id),
            )
            if status == "active":
                fts_text = (
                    f"{row['name']} {row['trigger_intent']} "
                    f"{row['prerequisites']} {row['steps_json']}"
                )
                self._sync_fts_entry(conn, skill_id, "skill", fts_text)
            else:
                self._sync_fts_entry(conn, skill_id, "skill", None)
            conn.commit()
            return True

    async def add_feedback(self, record: FeedbackRecord) -> FeedbackRecord:
        async with self._lock:
            conn = self._ensure_conn()
            cur = conn.execute(
                """
                INSERT INTO message_feedback (
                    turn_id, session_id, rating, profile, prompt, answer, source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.turn_id,
                    record.session_id,
                    record.rating,
                    record.profile,
                    record.prompt,
                    record.answer,
                    record.source,
                    record.created_at,
                ),
            )
            conn.commit()
            record.feedback_id = int(cur.lastrowid)
            return record

    async def add_work_history(self, record: WorkHistoryRecord) -> WorkHistoryRecord:
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                """
                INSERT INTO work_history (
                    work_id, session_id, turn_id, goal, result_summary,
                    trace_json, step_count, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.work_id,
                    record.session_id,
                    record.turn_id,
                    record.goal,
                    record.result_summary,
                    record.trace_json,
                    record.step_count,
                    record.status,
                    record.created_at,
                ),
            )
            conn.commit()
            return record

    async def get_work_history(self, work_id: str) -> Optional[WorkHistoryRecord]:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT * FROM work_history WHERE work_id = ?", (work_id,)
            ).fetchone()
            return _row_to_work_history(row) if row else None

    async def list_work_history(
        self,
        *,
        session_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[WorkHistoryRecord]:
        async with self._lock:
            conn = self._ensure_conn()
            if session_id:
                rows = conn.execute(
                    "SELECT * FROM work_history WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM work_history ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [_row_to_work_history(r) for r in rows]

    async def add_usage_record(self, record: UsageLedgerRecord) -> UsageLedgerRecord:
        async with self._lock:
            conn = self._ensure_conn()
            cached_tokens = int(record.cached_input_tokens or record.prompt_cache_hit_tokens or 0)
            cur = conn.execute(
                """
                INSERT INTO usage_ledger (
                    session_id, turn_id, profile, provider, slot, model,
                    prompt_tokens, prompt_cache_hit_tokens, cached_input_tokens,
                    completion_tokens, estimated_cost_cny, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.session_id,
                    record.turn_id,
                    record.profile,
                    record.provider,
                    record.slot,
                    record.model,
                    record.prompt_tokens,
                    cached_tokens,
                    cached_tokens,
                    record.completion_tokens,
                    record.estimated_cost_cny,
                    record.created_at,
                ),
            )
            conn.commit()
            return UsageLedgerRecord(
                ledger_id=int(cur.lastrowid),
                session_id=record.session_id,
                turn_id=record.turn_id,
                profile=record.profile,
                provider=record.provider,
                slot=record.slot,
                model=record.model,
                prompt_tokens=record.prompt_tokens,
                prompt_cache_hit_tokens=cached_tokens,
                cached_input_tokens=cached_tokens,
                completion_tokens=record.completion_tokens,
                estimated_cost_cny=record.estimated_cost_cny,
                created_at=record.created_at,
            )

    async def list_usage_records(self, *, limit: int = 50) -> List[UsageLedgerRecord]:
        async with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(
                "SELECT * FROM usage_ledger ORDER BY ledger_id DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
            return [
                UsageLedgerRecord(
                    ledger_id=int(r["ledger_id"]),
                    session_id=str(r["session_id"] or ""),
                    turn_id=str(r["turn_id"] or ""),
                    profile=str(r["profile"] or ""),
                    provider=str(r["provider"] or ""),
                    slot=str(r["slot"] or ""),
                    model=str(r["model"] or ""),
                    prompt_tokens=int(r["prompt_tokens"] or 0),
                    prompt_cache_hit_tokens=int(r["cached_input_tokens"] or r["prompt_cache_hit_tokens"] or 0),
                    cached_input_tokens=int(r["cached_input_tokens"] or r["prompt_cache_hit_tokens"] or 0),
                    completion_tokens=int(r["completion_tokens"] or 0),
                    estimated_cost_cny=float(r["estimated_cost_cny"] or 0.0),
                    created_at=float(r["created_at"] or 0.0),
                )
                for r in rows
            ]

    async def get_usage_summary(self, *, since_ts: float = 0.0) -> UsageSummary:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS calls,
                    COALESCE(SUM(prompt_tokens), 0) AS pt,
                    COALESCE(SUM(cached_input_tokens), 0) AS ht,
                    COALESCE(SUM(completion_tokens), 0) AS ct,
                    COALESCE(SUM(estimated_cost_cny), 0.0) AS cost
                FROM usage_ledger
                WHERE created_at >= ?
                """,
                (since_ts,),
            ).fetchone()
            calls = int(row["calls"]) if row else 0
            pt = int(row["pt"]) if row else 0
            ht = int(row["ht"]) if row else 0
            ct = int(row["ct"]) if row else 0
            cost = float(row["cost"]) if row else 0.0
            hit_rate = (ht / pt) if pt > 0 else 0.0
            return UsageSummary(
                calls=calls,
                prompt_tokens=pt,
                prompt_cache_hit_tokens=ht,
                cached_input_tokens=ht,
                completion_tokens=ct,
                total_cost_cny=cost,
                cache_hit_rate=hit_rate,
            )

    async def get_usage_by_slot(self, *, since_ts: float = 0.0) -> List[UsageSlotSummary]:
        async with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(
                """
                SELECT
                    slot,
                    COUNT(*) AS calls,
                    COALESCE(SUM(prompt_tokens), 0) AS pt,
                    COALESCE(SUM(cached_input_tokens), 0) AS ht,
                    COALESCE(SUM(completion_tokens), 0) AS ct,
                    COALESCE(SUM(estimated_cost_cny), 0.0) AS cost
                FROM usage_ledger
                WHERE created_at >= ?
                GROUP BY slot
                ORDER BY slot
                """,
                (since_ts,),
            ).fetchall()
            result: List[UsageSlotSummary] = []
            for row in rows:
                pt = int(row["pt"] or 0)
                ht = int(row["ht"] or 0)
                result.append(UsageSlotSummary(
                    slot=str(row["slot"] or ""),
                    calls=int(row["calls"] or 0),
                    prompt_tokens=pt,
                    cached_input_tokens=ht,
                    completion_tokens=int(row["ct"] or 0),
                    total_cost_cny=float(row["cost"] or 0.0),
                    cache_hit_rate=(ht / pt) if pt > 0 else 0.0,
                ))
            return result

    # --- M5: Scheduled Tasks ---

    async def add_scheduled_task(self, task: ScheduledTaskRecord) -> ScheduledTaskRecord:
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                """
                INSERT INTO scheduled_tasks (
                    task_id, title, task_type, schedule_kind, schedule_expr,
                    payload_json, next_run_at, last_run_at, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.title,
                    task.task_type,
                    task.schedule_kind,
                    task.schedule_expr,
                    task.payload_json,
                    task.next_run_at,
                    task.last_run_at,
                    task.status,
                    task.created_at,
                ),
            )
            conn.commit()
            return task

    async def get_scheduled_task(self, task_id: str) -> Optional[ScheduledTaskRecord]:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            return _row_to_scheduled_task(row) if row else None

    async def list_scheduled_tasks(
        self,
        *,
        status: Optional[TaskStatus] = "active",
        limit: int = 50,
    ) -> List[ScheduledTaskRecord]:
        async with self._lock:
            conn = self._ensure_conn()
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM scheduled_tasks ORDER BY next_run_at ASC, created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM scheduled_tasks WHERE status = ? ORDER BY next_run_at ASC, created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            return [_row_to_scheduled_task(r) for r in rows]

    async def list_due_tasks(self, *, now: float) -> List[ScheduledTaskRecord]:
        async with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(
                """
                SELECT * FROM scheduled_tasks
                WHERE status = 'active' AND next_run_at <= ?
                ORDER BY next_run_at ASC
                """,
                (now,),
            ).fetchall()
            return [_row_to_scheduled_task(r) for r in rows]

    async def update_scheduled_task(
        self,
        task_id: str,
        *,
        status: Optional[TaskStatus] = None,
        next_run_at: Optional[float] = None,
        last_run_at: Optional[float] = None,
    ) -> Optional[ScheduledTaskRecord]:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            new_status = status if status is not None else row["status"]
            new_next = next_run_at if next_run_at is not None else float(row["next_run_at"])
            new_last = last_run_at if last_run_at is not None else row["last_run_at"]
            conn.execute(
                """
                UPDATE scheduled_tasks
                SET status = ?, next_run_at = ?, last_run_at = ?
                WHERE task_id = ?
                """,
                (new_status, new_next, new_last, task_id),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            return _row_to_scheduled_task(updated) if updated else None

    # --- GA 1.0: Background Long Tasks ---

    async def add_background_task(
        self, task: BackgroundTaskRecord
    ) -> BackgroundTaskRecord:
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                """
                INSERT INTO background_tasks (
                    task_id, title, goal_prompt, source_session_id, status,
                    progress_pct, current_step, result_summary, artifact_path,
                    bundle_path, result_files_json, error_message, slot_used,
                    worker_session_id, origin, mandate_json, checkpoint_json,
                    result_refs_json, watchdog_state_json, last_heartbeat_at,
                    created_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.title,
                    task.goal_prompt,
                    task.source_session_id,
                    task.status,
                    int(task.progress_pct),
                    task.current_step,
                    task.result_summary,
                    task.artifact_path,
                    task.bundle_path,
                    json.dumps(task.result_files, ensure_ascii=False),
                    task.error_message,
                    task.slot_used,
                    task.worker_session_id,
                    task.origin,
                    json.dumps(task.mandate, ensure_ascii=False),
                    json.dumps(task.checkpoint, ensure_ascii=False),
                    json.dumps(task.result_refs, ensure_ascii=False),
                    json.dumps(task.watchdog_state, ensure_ascii=False),
                    task.last_heartbeat_at,
                    task.created_at,
                    task.started_at,
                    task.finished_at,
                ),
            )
            conn.commit()
            return task

    async def get_background_task(
        self, task_id: str
    ) -> Optional[BackgroundTaskRecord]:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT * FROM background_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            return _row_to_background_task(row) if row else None

    async def list_background_tasks(
        self,
        *,
        status: Optional[BackgroundTaskStatus] = None,
        limit: int = 50,
    ) -> List[BackgroundTaskRecord]:
        async with self._lock:
            conn = self._ensure_conn()
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM background_tasks ORDER BY created_at DESC LIMIT ?",
                    (max(1, limit),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM background_tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, max(1, limit)),
                ).fetchall()
            return [_row_to_background_task(r) for r in rows]

    async def update_background_task(
        self,
        task_id: str,
        *,
        status: Optional[BackgroundTaskStatus] = None,
        progress_pct: Optional[int] = None,
        current_step: Optional[str] = None,
        result_summary: Optional[str] = None,
        artifact_path: Optional[str] = None,
        bundle_path: Optional[str] = None,
        result_files: Optional[List[str]] = None,
        error_message: Optional[str] = None,
        slot_used: Optional[str] = None,
        worker_session_id: Optional[str] = None,
        origin: Optional[str] = None,
        mandate: Optional[Dict[str, Any]] = None,
        checkpoint: Optional[Dict[str, Any]] = None,
        result_refs: Optional[List[str]] = None,
        watchdog_state: Optional[Dict[str, Any]] = None,
        last_heartbeat_at: Optional[float] = None,
        started_at: Optional[float] = None,
        finished_at: Optional[float] = None,
    ) -> Optional[BackgroundTaskRecord]:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT * FROM background_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            new_status = status if status is not None else row["status"]
            new_progress = (
                max(0, min(100, int(progress_pct)))
                if progress_pct is not None
                else int(row["progress_pct"])
            )
            new_step = current_step if current_step is not None else row["current_step"]
            new_summary = (
                result_summary if result_summary is not None else row["result_summary"]
            )
            new_artifact = (
                artifact_path if artifact_path is not None else row["artifact_path"]
            )
            new_bundle = bundle_path if bundle_path is not None else row["bundle_path"]
            new_result_files = (
                json.dumps(result_files, ensure_ascii=False)
                if result_files is not None
                else row["result_files_json"]
            )
            new_error = error_message if error_message is not None else row["error_message"]
            new_slot = slot_used if slot_used is not None else row["slot_used"]
            new_worker = worker_session_id if worker_session_id is not None else row["worker_session_id"]
            new_origin = origin if origin is not None else row["origin"]
            new_mandate = json.dumps(mandate, ensure_ascii=False) if mandate is not None else row["mandate_json"]
            new_checkpoint = json.dumps(checkpoint, ensure_ascii=False) if checkpoint is not None else row["checkpoint_json"]
            new_refs = json.dumps(result_refs, ensure_ascii=False) if result_refs is not None else row["result_refs_json"]
            new_watchdog = json.dumps(watchdog_state, ensure_ascii=False) if watchdog_state is not None else row["watchdog_state_json"]
            new_heartbeat = last_heartbeat_at if last_heartbeat_at is not None else row["last_heartbeat_at"]
            new_started = started_at if started_at is not None else row["started_at"]
            new_finished = finished_at if finished_at is not None else row["finished_at"]
            conn.execute(
                """
                UPDATE background_tasks
                SET status = ?, progress_pct = ?, current_step = ?, result_summary = ?,
                    artifact_path = ?, bundle_path = ?, result_files_json = ?,
                    error_message = ?, slot_used = ?,
                    worker_session_id = ?, origin = ?, mandate_json = ?,
                    checkpoint_json = ?, result_refs_json = ?, watchdog_state_json = ?,
                    last_heartbeat_at = ?, started_at = ?, finished_at = ?
                WHERE task_id = ?
                """,
                (
                    new_status,
                    new_progress,
                    new_step,
                    new_summary,
                    new_artifact,
                    new_bundle,
                    new_result_files,
                    new_error,
                    new_slot,
                    new_worker,
                    new_origin,
                    new_mandate,
                    new_checkpoint,
                    new_refs,
                    new_watchdog,
                    new_heartbeat,
                    new_started,
                    new_finished,
                    task_id,
                ),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM background_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            return _row_to_background_task(updated) if updated else None

    async def add_task_inbox(
        self, task_id: str, *, sender: str, content: str
    ) -> TaskInboxRecord:
        async with self._lock:
            conn = self._ensure_conn()
            now = time.time()
            cur = conn.execute(
                "INSERT INTO task_inbox(task_id, sender, content, created_at) VALUES (?, ?, ?, ?)",
                (task_id, sender, content, now),
            )
            conn.commit()
            return TaskInboxRecord(
                id=int(cur.lastrowid), task_id=task_id, sender=sender,
                content=content, created_at=now,
            )

    async def consume_task_inbox(self, task_id: str) -> List[TaskInboxRecord]:
        async with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(
                "SELECT * FROM task_inbox WHERE task_id = ? AND consumed_at IS NULL ORDER BY id ASC",
                (task_id,),
            ).fetchall()
            if not rows:
                return []
            now = time.time()
            conn.executemany(
                "UPDATE task_inbox SET consumed_at = ? WHERE id = ?",
                [(now, int(row["id"])) for row in rows],
            )
            conn.commit()
            return [
                TaskInboxRecord(
                    id=int(row["id"]), task_id=str(row["task_id"]),
                    sender=str(row["sender"]), content=str(row["content"]),
                    created_at=float(row["created_at"]), consumed_at=now,
                )
                for row in rows
            ]

    async def acquire_resource_lock(
        self,
        resource_key: str,
        task_id: str,
        *,
        mode: str = "exclusive",
        lease_seconds: float = 120.0,
    ) -> bool:
        async with self._lock:
            conn = self._ensure_conn()
            now = time.time()
            conn.execute("DELETE FROM resource_locks WHERE lease_expires_at <= ?", (now,))
            rows = conn.execute(
                "SELECT * FROM resource_locks WHERE resource_key = ?", (resource_key,)
            ).fetchall()
            for row in rows:
                if str(row["task_id"]) == task_id:
                    continue
                if mode == "exclusive" or str(row["mode"]) == "exclusive":
                    conn.commit()
                    return False
            conn.execute(
                """INSERT OR REPLACE INTO resource_locks
                   (resource_key, task_id, mode, acquired_at, lease_expires_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (resource_key, task_id, mode, now, now + max(15.0, lease_seconds)),
            )
            conn.commit()
            return True

    async def renew_resource_lock(
        self, resource_key: str, task_id: str, *, lease_seconds: float = 120.0
    ) -> bool:
        async with self._lock:
            conn = self._ensure_conn()
            cur = conn.execute(
                "UPDATE resource_locks SET lease_expires_at = ? WHERE resource_key = ? AND task_id = ?",
                (time.time() + max(15.0, lease_seconds), resource_key, task_id),
            )
            conn.commit()
            return bool(cur.rowcount)

    async def release_resource_lock(self, resource_key: str, task_id: str) -> None:
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                "DELETE FROM resource_locks WHERE resource_key = ? AND task_id = ?",
                (resource_key, task_id),
            )
            conn.commit()

    async def cleanup_expired_resource_locks(self) -> int:
        async with self._lock:
            conn = self._ensure_conn()
            cur = conn.execute("DELETE FROM resource_locks WHERE lease_expires_at <= ?", (time.time(),))
            conn.commit()
            return int(cur.rowcount or 0)

    async def list_resource_locks(self, task_id: str = "") -> List[ResourceLockRecord]:
        async with self._lock:
            conn = self._ensure_conn()
            if task_id:
                rows = conn.execute(
                    "SELECT * FROM resource_locks WHERE task_id = ? ORDER BY resource_key",
                    (task_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM resource_locks ORDER BY resource_key").fetchall()
            return [
                ResourceLockRecord(
                    resource_key=str(r["resource_key"]), task_id=str(r["task_id"]),
                    mode=str(r["mode"]), acquired_at=float(r["acquired_at"]),
                    lease_expires_at=float(r["lease_expires_at"]),
                )
                for r in rows
            ]

    async def mark_unfinished_background_tasks_interrupted(
        self,
        *,
        reason: str = "服务重启，任务已中断",
    ) -> int:
        async with self._lock:
            conn = self._ensure_conn()
            now = time.time()
            cur = conn.execute(
                """
                UPDATE background_tasks
                SET status = 'interrupted',
                    error_message = CASE WHEN error_message = '' THEN ? ELSE error_message END,
                    finished_at = COALESCE(finished_at, ?)
                WHERE status IN ('queued', 'planning', 'running', 'waiting_approval', 'waiting_input', 'verifying')
                """,
                (reason, now),
            )
            conn.commit()
            return int(cur.rowcount or 0)

    # --- Lossless JSONL Snapshot Export / Import ---

    async def list_referenced_media_paths(self) -> set[str]:
        async with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(
                "SELECT media_json FROM messages UNION ALL SELECT media_json FROM messages_archive"
            ).fetchall()
        paths: set[str] = set()
        for row in rows:
            try:
                items = json.loads(str(row["media_json"] or "[]"))
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("local_path") or "").strip()
                if path:
                    paths.add(path)
        return paths

    async def export_snapshot(self, output_path: Path) -> int:
        async with self._lock:
            conn = self._ensure_conn()
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            count = 0
            with output_path.open("w", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "_type": "meta",
                            "schema_version": SNAPSHOT_SCHEMA_VERSION,
                            "exported_at": time.time(),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                for r in conn.execute("SELECT * FROM memory_categories ORDER BY created_at ASC"):
                    f.write(
                        json.dumps({"_type": "category", **dict(r)}, ensure_ascii=False) + "\n"
                    )
                    count += 1
                for r in conn.execute("SELECT * FROM sessions ORDER BY started_at ASC"):
                    f.write(
                        json.dumps({"_type": "session", **dict(r)}, ensure_ascii=False) + "\n"
                    )
                    count += 1
                for r in conn.execute("SELECT * FROM messages ORDER BY msg_id ASC"):
                    f.write(
                        json.dumps({"_type": "message", **dict(r)}, ensure_ascii=False) + "\n"
                    )
                    count += 1
                for r in conn.execute("SELECT * FROM messages_archive ORDER BY msg_id ASC"):
                    f.write(
                        json.dumps({"_type": "message_archive", **dict(r)}, ensure_ascii=False)
                        + "\n"
                    )
                    count += 1
                for r in conn.execute("SELECT * FROM memory_facts ORDER BY created_at ASC"):
                    d = dict(r)
                    if d.get("embedding") is not None:
                        d["embedding_b64"] = base64.b64encode(d.pop("embedding")).decode("ascii")
                    else:
                        d.pop("embedding", None)
                        d["embedding_b64"] = None
                    f.write(json.dumps({"_type": "fact", **d}, ensure_ascii=False) + "\n")
                    count += 1
                for r in conn.execute("SELECT * FROM memory_episodes ORDER BY created_at ASC"):
                    d = dict(r)
                    if d.get("embedding") is not None:
                        d["embedding_b64"] = base64.b64encode(d.pop("embedding")).decode("ascii")
                    else:
                        d.pop("embedding", None)
                        d["embedding_b64"] = None
                    f.write(json.dumps({"_type": "episode", **d}, ensure_ascii=False) + "\n")
                    count += 1
                for r in conn.execute("SELECT * FROM memory_skills ORDER BY updated_at ASC"):
                    f.write(json.dumps({"_type": "skill", **dict(r)}, ensure_ascii=False) + "\n")
                    count += 1
                for r in conn.execute("SELECT * FROM work_history ORDER BY created_at ASC"):
                    f.write(
                        json.dumps({"_type": "work_history", **dict(r)}, ensure_ascii=False) + "\n"
                    )
                    count += 1
                for r in conn.execute("SELECT * FROM usage_ledger ORDER BY ledger_id ASC"):
                    f.write(json.dumps({"_type": "usage", **dict(r)}, ensure_ascii=False) + "\n")
                    count += 1
                for r in conn.execute("SELECT * FROM scheduled_tasks ORDER BY created_at ASC"):
                    f.write(
                        json.dumps({"_type": "scheduled_task", **dict(r)}, ensure_ascii=False)
                        + "\n"
                    )
                    count += 1
                for r in conn.execute("SELECT * FROM background_tasks ORDER BY created_at ASC"):
                    f.write(
                        json.dumps({"_type": "background_task", **dict(r)}, ensure_ascii=False)
                        + "\n"
                    )
                    count += 1
            return count

    async def import_snapshot(self, input_path: Path) -> int:
        async with self._lock:
            conn = self._ensure_conn()
            count = 0
            with Path(input_path).open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    rtype = rec.get("_type")
                    if rtype == "meta":
                        continue
                    elif rtype == "category":
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO memory_categories (
                                category_key, display_name, description, tier,
                                retrieval_weight, decay_rate, inject_to_l1, created_by, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                rec["category_key"],
                                rec["display_name"],
                                rec.get("description", ""),
                                rec.get("tier", "episodic"),
                                rec.get("retrieval_weight", 0.85),
                                rec.get("decay_rate", 0.05),
                                int(rec.get("inject_to_l1", 0)),
                                rec.get("created_by", "system"),
                                rec["created_at"],
                            ),
                        )
                        count += 1
                    elif rtype == "session":
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO sessions (
                                session_id, started_at, updated_at, rolling_summary,
                                last_summarized_msg_id, status
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                rec["session_id"],
                                rec["started_at"],
                                rec["updated_at"],
                                rec.get("rolling_summary", ""),
                                rec.get("last_summarized_msg_id", 0),
                                rec.get("status", "active"),
                            ),
                        )
                        count += 1
                    elif rtype == "message":
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO messages (
                                msg_id, session_id, turn_id, role, content,
                                emotion, desktop_context_summary, sources_json,
                                media_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                rec["msg_id"],
                                rec["session_id"],
                                rec.get("turn_id", ""),
                                rec["role"],
                                rec["content"],
                                rec.get("emotion", "neutral"),
                                rec.get("desktop_context_summary", ""),
                                json.dumps(_snapshot_rich(rec, "sources"), ensure_ascii=False),
                                json.dumps(_snapshot_rich(rec, "media"), ensure_ascii=False),
                                rec["created_at"],
                            ),
                        )
                        if len(str(rec["content"]).strip()) >= 6:
                            self._sync_fts_entry(
                                conn, str(rec["msg_id"]), "message", str(rec["content"])
                            )
                        count += 1
                    elif rtype == "message_archive":
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO messages_archive (
                                msg_id, session_id, turn_id, role, content,
                                emotion, desktop_context_summary, sources_json,
                                media_json, created_at, archived_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                rec["msg_id"],
                                rec["session_id"],
                                rec.get("turn_id", ""),
                                rec["role"],
                                rec["content"],
                                rec.get("emotion", "neutral"),
                                rec.get("desktop_context_summary", ""),
                                json.dumps(_snapshot_rich(rec, "sources"), ensure_ascii=False),
                                json.dumps(_snapshot_rich(rec, "media"), ensure_ascii=False),
                                rec["created_at"],
                                rec.get("archived_at", rec["created_at"]),
                            ),
                        )
                        count += 1
                    elif rtype == "fact":
                        emb_b64 = rec.get("embedding_b64")
                        emb = base64.b64decode(emb_b64) if emb_b64 else None
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO memory_facts (
                                fact_id, category, content, importance, confidence, status,
                                source_session_id, created_at, updated_at, last_accessed_at,
                                embedding, embedding_model
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                rec["fact_id"],
                                rec["category"],
                                rec["content"],
                                rec.get("importance", 0.7),
                                rec.get("confidence", 0.9),
                                rec.get("status", "active"),
                                rec.get("source_session_id", ""),
                                rec["created_at"],
                                rec["updated_at"],
                                rec.get("last_accessed_at", rec["updated_at"]),
                                emb,
                                rec.get("embedding_model"),
                            ),
                        )
                        if rec.get("status", "active") == "active":
                            self._sync_fts_entry(
                                conn,
                                rec["fact_id"],
                                "fact",
                                f"{rec['category']} {rec['content']}",
                            )
                        count += 1
                    elif rtype == "episode":
                        emb_b64 = rec.get("embedding_b64")
                        emb = base64.b64decode(emb_b64) if emb_b64 else None
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO memory_episodes (
                                episode_id, kind, summary, keywords, importance,
                                time_start, time_end, status, created_at,
                                embedding, embedding_model
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                rec["episode_id"],
                                rec["kind"],
                                rec["summary"],
                                rec.get("keywords", ""),
                                rec.get("importance", 0.6),
                                rec["time_start"],
                                rec["time_end"],
                                rec.get("status", "active"),
                                rec["created_at"],
                                emb,
                                rec.get("embedding_model"),
                            ),
                        )
                        if rec.get("status", "active") == "active":
                            self._sync_fts_entry(
                                conn,
                                rec["episode_id"],
                                "episode",
                                f"{rec.get('keywords', '')} {rec['summary']}",
                            )
                        count += 1
                    elif rtype == "skill":
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO memory_skills (
                                skill_id, name, trigger_intent, tool_preference, prerequisites,
                                steps_json, success_count, failure_count, status, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                rec["skill_id"],
                                rec["name"],
                                rec["trigger_intent"],
                                rec.get("tool_preference", "shell"),
                                rec.get("prerequisites", ""),
                                rec.get("steps_json", "[]"),
                                rec.get("success_count", 1),
                                rec.get("failure_count", 0),
                                rec.get("status", "active"),
                                rec["updated_at"],
                            ),
                        )
                        if rec.get("status", "active") == "active":
                            self._sync_fts_entry(
                                conn,
                                rec["skill_id"],
                                "skill",
                                f"{rec['name']} {rec['trigger_intent']} {rec.get('prerequisites', '')} {rec.get('steps_json', '[]')}",
                            )
                        count += 1
                    elif rtype == "work_history":
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO work_history (
                                work_id, session_id, turn_id, goal, result_summary,
                                trace_json, step_count, status, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                rec["work_id"],
                                rec.get("session_id", ""),
                                rec.get("turn_id", ""),
                                rec["goal"],
                                rec["result_summary"],
                                rec.get("trace_json", "[]"),
                                rec.get("step_count", 0),
                                rec.get("status", "completed"),
                                rec["created_at"],
                            ),
                        )
                        count += 1
                    elif rtype == "usage":
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO usage_ledger (
                                ledger_id, session_id, turn_id, profile, provider, slot, model,
                                prompt_tokens, prompt_cache_hit_tokens, cached_input_tokens,
                                completion_tokens, estimated_cost_cny, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                rec["ledger_id"],
                                rec.get("session_id", ""),
                                rec.get("turn_id", ""),
                                rec.get("profile", ""),
                                rec.get("provider", ""),
                                rec.get("slot", ""),
                                rec.get("model", ""),
                                rec.get("prompt_tokens", 0),
                                int(rec.get("cached_input_tokens") or rec.get("prompt_cache_hit_tokens") or 0),
                                int(rec.get("cached_input_tokens") or rec.get("prompt_cache_hit_tokens") or 0),
                                rec.get("completion_tokens", 0),
                                rec.get("estimated_cost_cny", 0.0),
                                rec["created_at"],
                            ),
                        )
                        count += 1
                    elif rtype == "scheduled_task":
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO scheduled_tasks (
                                task_id, title, task_type, schedule_kind, schedule_expr,
                                payload_json, next_run_at, last_run_at, status, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                rec["task_id"],
                                rec["title"],
                                rec.get("task_type", "reminder"),
                                rec.get("schedule_kind", "once"),
                                rec.get("schedule_expr", ""),
                                rec.get("payload_json", "{}"),
                                rec["next_run_at"],
                                rec.get("last_run_at"),
                                rec.get("status", "active"),
                                rec["created_at"],
                            ),
                        )
                        count += 1
                    elif rtype == "background_task":
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO background_tasks (
                                task_id, title, goal_prompt, source_session_id, status,
                                progress_pct, current_step, result_summary, artifact_path,
                                bundle_path, result_files_json, error_message, slot_used,
                                worker_session_id, origin, mandate_json, checkpoint_json,
                                result_refs_json, watchdog_state_json, last_heartbeat_at,
                                created_at, started_at, finished_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                rec["task_id"],
                                rec["title"],
                                rec.get("goal_prompt", ""),
                                rec.get("source_session_id", ""),
                                rec.get("status", "interrupted"),
                                int(rec.get("progress_pct", 0)),
                                rec.get("current_step", ""),
                                rec.get("result_summary", ""),
                                rec.get("artifact_path", ""),
                                rec.get("bundle_path", ""),
                                rec.get("result_files_json", "[]"),
                                rec.get("error_message", ""),
                                rec.get("slot_used", "bg_task"),
                                rec.get("worker_session_id", ""),
                                rec.get("origin", "explicit_user"),
                                rec.get("mandate_json", "{}"),
                                rec.get("checkpoint_json", "{}"),
                                rec.get("result_refs_json", "[]"),
                                rec.get("watchdog_state_json", "{}"),
                                rec.get("last_heartbeat_at", 0),
                                rec["created_at"],
                                rec.get("started_at"),
                                rec.get("finished_at"),
                            ),
                        )
                        count += 1
            conn.commit()
            return count


def _row_to_session(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        session_id=str(row["session_id"]),
        started_at=float(row["started_at"]),
        updated_at=float(row["updated_at"]),
        rolling_summary=str(row["rolling_summary"] or ""),
        last_summarized_msg_id=int(row["last_summarized_msg_id"] or 0),
        status=row["status"],  # type: ignore[arg-type]
    )


def _snapshot_rich(record: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    value = record.get(key)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    raw = record.get(f"{key}_json")
    if isinstance(raw, str):
        return _parse_json_list(raw)
    return []


def _parse_json_list(raw: Any) -> List[Dict[str, Any]]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except (json.JSONDecodeError, TypeError):
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _row_to_message(row: sqlite3.Row) -> MessageRecord:
    return MessageRecord(
        msg_id=int(row["msg_id"]),
        session_id=str(row["session_id"]),
        turn_id=str(row["turn_id"] or ""),
        role=str(row["role"]),
        content=str(row["content"]),
        emotion=str(row["emotion"] or "neutral"),
        desktop_context_summary=str(row["desktop_context_summary"] or ""),
        sources=_parse_json_list(row["sources_json"] if "sources_json" in row.keys() else "[]"),
        media=_parse_json_list(row["media_json"] if "media_json" in row.keys() else "[]"),
        created_at=float(row["created_at"]),
    )


def _row_to_fact(row: sqlite3.Row) -> MemoryFact:
    return MemoryFact(
        fact_id=str(row["fact_id"]),
        category=str(row["category"]),
        content=str(row["content"]),
        importance=float(row["importance"]),
        confidence=float(row["confidence"]),
        status=row["status"],  # type: ignore[arg-type]
        source_session_id=str(row["source_session_id"] or ""),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        last_accessed_at=float(row["last_accessed_at"]),
        embedding=bytes(row["embedding"]) if row["embedding"] is not None else None,
        embedding_model=str(row["embedding_model"]) if row["embedding_model"] else None,
    )


def _row_to_skill(row: sqlite3.Row) -> MemorySkill:
    return MemorySkill(
        skill_id=str(row["skill_id"]),
        name=str(row["name"]),
        trigger_intent=str(row["trigger_intent"]),
        tool_preference=str(row["tool_preference"] or "shell"),
        prerequisites=str(row["prerequisites"] or ""),
        steps_json=str(row["steps_json"] or "[]"),
        success_count=int(row["success_count"] or 0),
        failure_count=int(row["failure_count"] or 0),
        status=row["status"],  # type: ignore[arg-type]
        updated_at=float(row["updated_at"]),
    )


def _row_to_work_history(row: sqlite3.Row) -> WorkHistoryRecord:
    return WorkHistoryRecord(
        work_id=str(row["work_id"]),
        session_id=str(row["session_id"] or ""),
        turn_id=str(row["turn_id"] or ""),
        goal=str(row["goal"]),
        result_summary=str(row["result_summary"]),
        trace_json=str(row["trace_json"] or "[]"),
        step_count=int(row["step_count"] or 0),
        status=str(row["status"] or "completed"),
        created_at=float(row["created_at"]),
    )


def _row_to_scheduled_task(row: sqlite3.Row) -> ScheduledTaskRecord:
    return ScheduledTaskRecord(
        task_id=str(row["task_id"]),
        title=str(row["title"]),
        task_type=row["task_type"],  # type: ignore[arg-type]
        schedule_kind=row["schedule_kind"],  # type: ignore[arg-type]
        schedule_expr=str(row["schedule_expr"] or ""),
        payload_json=str(row["payload_json"] or "{}"),
        next_run_at=float(row["next_run_at"]),
        last_run_at=float(row["last_run_at"]) if row["last_run_at"] is not None else None,
        status=row["status"],  # type: ignore[arg-type]
        created_at=float(row["created_at"]),
    )


def _parse_json_dict(raw: Any) -> Dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_str_list(raw: Any) -> List[str]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def _row_to_background_task(row: sqlite3.Row) -> BackgroundTaskRecord:
    keys = set(row.keys())
    return BackgroundTaskRecord(
        task_id=str(row["task_id"]),
        title=str(row["title"]),
        goal_prompt=str(row["goal_prompt"] or ""),
        source_session_id=str(row["source_session_id"] or ""),
        status=row["status"],  # type: ignore[arg-type]
        progress_pct=int(row["progress_pct"] or 0),
        current_step=str(row["current_step"] or ""),
        result_summary=str(row["result_summary"] or ""),
        artifact_path=str(row["artifact_path"] or ""),
        bundle_path=str(row["bundle_path"] or "") if "bundle_path" in keys else "",
        result_files=_parse_str_list(row["result_files_json"]) if "result_files_json" in keys else [],
        error_message=str(row["error_message"] or ""),
        slot_used=str(row["slot_used"] or "bg_task"),
        worker_session_id=str(row["worker_session_id"] or "") if "worker_session_id" in keys else "",
        origin=str(row["origin"] or "explicit_user") if "origin" in keys else "explicit_user",
        mandate=_parse_json_dict(row["mandate_json"]) if "mandate_json" in keys else {},
        checkpoint=_parse_json_dict(row["checkpoint_json"]) if "checkpoint_json" in keys else {},
        result_refs=_parse_str_list(row["result_refs_json"]) if "result_refs_json" in keys else [],
        watchdog_state=_parse_json_dict(row["watchdog_state_json"]) if "watchdog_state_json" in keys else {},
        last_heartbeat_at=float(row["last_heartbeat_at"] or 0) if "last_heartbeat_at" in keys else 0.0,
        created_at=float(row["created_at"]),
        started_at=float(row["started_at"]) if row["started_at"] is not None else None,
        finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
    )


def _row_to_category(row: sqlite3.Row) -> MemoryCategory:
    return MemoryCategory(
        category_key=str(row["category_key"]),
        display_name=str(row["display_name"]),
        description=str(row["description"] or ""),
        tier=row["tier"],  # type: ignore[arg-type]
        retrieval_weight=float(row["retrieval_weight"]),
        decay_rate=float(row["decay_rate"]),
        inject_to_l1=bool(row["inject_to_l1"]),
        created_by=str(row["created_by"] or "system"),
        created_at=float(row["created_at"]),
    )


def _row_to_episode(row: sqlite3.Row) -> MemoryEpisode:
    return MemoryEpisode(
        episode_id=str(row["episode_id"]),
        kind=row["kind"],  # type: ignore[arg-type]
        summary=str(row["summary"]),
        keywords=str(row["keywords"] or ""),
        importance=float(row["importance"]),
        time_start=float(row["time_start"]),
        time_end=float(row["time_end"]),
        status=row["status"],  # type: ignore[arg-type]
        created_at=float(row["created_at"]),
        embedding=bytes(row["embedding"]) if row["embedding"] is not None else None,
        embedding_model=str(row["embedding_model"]) if row["embedding_model"] else None,
    )


def _row_to_skill(row: sqlite3.Row) -> MemorySkill:
    return MemorySkill(
        skill_id=str(row["skill_id"]),
        name=str(row["name"]),
        trigger_intent=str(row["trigger_intent"]),
        tool_preference=str(row["tool_preference"] or "shell"),
        prerequisites=str(row["prerequisites"] or ""),
        steps_json=str(row["steps_json"] or "[]"),
        success_count=int(row["success_count"] or 0),
        failure_count=int(row["failure_count"] or 0),
        status=row["status"],  # type: ignore[arg-type]
        updated_at=float(row["updated_at"]),
    )


def _row_to_work_history(row: sqlite3.Row) -> WorkHistoryRecord:
    return WorkHistoryRecord(
        work_id=str(row["work_id"]),
        session_id=str(row["session_id"] or ""),
        turn_id=str(row["turn_id"] or ""),
        goal=str(row["goal"]),
        result_summary=str(row["result_summary"]),
        trace_json=str(row["trace_json"] or "[]"),
        step_count=int(row["step_count"] or 0),
        status=str(row["status"] or "completed"),
        created_at=float(row["created_at"]),
    )


def _row_to_scheduled_task(row: sqlite3.Row) -> ScheduledTaskRecord:
    return ScheduledTaskRecord(
        task_id=str(row["task_id"]),
        title=str(row["title"]),
        task_type=row["task_type"],  # type: ignore[arg-type]
        schedule_kind=row["schedule_kind"],  # type: ignore[arg-type]
        schedule_expr=str(row["schedule_expr"] or ""),
        payload_json=str(row["payload_json"] or "{}"),
        next_run_at=float(row["next_run_at"]),
        last_run_at=float(row["last_run_at"]) if row["last_run_at"] is not None else None,
        status=row["status"],  # type: ignore[arg-type]
        created_at=float(row["created_at"]),
    )


def _row_to_background_task(row: sqlite3.Row) -> BackgroundTaskRecord:
    keys = set(row.keys())
    return BackgroundTaskRecord(
        task_id=str(row["task_id"]),
        title=str(row["title"]),
        goal_prompt=str(row["goal_prompt"] or ""),
        source_session_id=str(row["source_session_id"] or ""),
        status=row["status"],  # type: ignore[arg-type]
        progress_pct=int(row["progress_pct"] or 0),
        current_step=str(row["current_step"] or ""),
        result_summary=str(row["result_summary"] or ""),
        artifact_path=str(row["artifact_path"] or ""),
        bundle_path=str(row["bundle_path"] or "") if "bundle_path" in keys else "",
        result_files=_parse_str_list(row["result_files_json"]) if "result_files_json" in keys else [],
        error_message=str(row["error_message"] or ""),
        slot_used=str(row["slot_used"] or "bg_task"),
        worker_session_id=str(row["worker_session_id"] or "") if "worker_session_id" in keys else "",
        origin=str(row["origin"] or "explicit_user") if "origin" in keys else "explicit_user",
        mandate=_parse_json_dict(row["mandate_json"]) if "mandate_json" in keys else {},
        checkpoint=_parse_json_dict(row["checkpoint_json"]) if "checkpoint_json" in keys else {},
        result_refs=_parse_str_list(row["result_refs_json"]) if "result_refs_json" in keys else [],
        watchdog_state=_parse_json_dict(row["watchdog_state_json"]) if "watchdog_state_json" in keys else {},
        last_heartbeat_at=float(row["last_heartbeat_at"] or 0) if "last_heartbeat_at" in keys else 0.0,
        created_at=float(row["created_at"]),
        started_at=float(row["started_at"]) if row["started_at"] is not None else None,
        finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
    )
