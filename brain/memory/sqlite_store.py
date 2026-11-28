from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from brain.memory.schema import _SCHEMA_SQL
from brain.memory.rows import (
    _snapshot_rich,
    _row_to_session,
    _parse_json_list,
    _row_to_message,
    _row_to_fact,
    _row_to_skill,
    _row_to_work_history,
    _row_to_scheduled_task,
    _parse_json_dict,
    _row_to_background_task,
    _row_to_category,
    _row_to_episode,
    _row_to_skill,
    _row_to_work_history,
    _row_to_scheduled_task,
    _row_to_background_task,
)
from brain.memory.messages import MessagesMixin
from brain.memory.facts import FactsMixin
from brain.memory.skills_usage import SkillsUsageMixin
from brain.memory.tasks_store import TasksStoreMixin
from brain.memory.snapshot import SnapshotMixin
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

class SQLiteMemoryStore(
    MessagesMixin,
    FactsMixin,
    SkillsUsageMixin,
    TasksStoreMixin,
    SnapshotMixin,
    MemoryBackend
):
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
                if "cards_json" not in msg_cols:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN cards_json TEXT NOT NULL DEFAULT '[]'"
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
