from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import time
from pathlib import Path
from typing import List, Optional, Tuple

from brain.memory.backend import SNAPSHOT_SCHEMA_VERSION, MemoryBackend
from brain.memory.models import (
    DEFAULT_MEMORY_CATEGORIES,
    EpisodeKind,
    EpisodeStatus,
    FactStatus,
    MemoryCategory,
    MemoryEpisode,
    MemoryFact,
    MessageRecord,
    SessionRecord,
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
    created_at REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session_msg
    ON messages(session_id, msg_id);

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
    created_at REAL NOT NULL,
    archived_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_archive_session
    ON messages_archive(session_id, msg_id);

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
    ) -> MessageRecord:
        async with self._lock:
            conn = self._ensure_conn()
            now = time.time()
            cur = conn.execute(
                """
                INSERT INTO messages (session_id, turn_id, role, content, emotion, desktop_context_summary, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, turn_id, role, content, emotion, desktop_context_summary, now),
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
                    emotion, desktop_context_summary, created_at, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    "SELECT * FROM memory_facts ORDER BY importance DESC, updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM memory_facts
                    WHERE status = ?
                    ORDER BY importance DESC, updated_at DESC
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
                    WHERE memory_fts MATCH ?
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

    # --- Lossless JSONL Snapshot Export / Import ---

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
                                emotion, desktop_context_summary, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                rec["msg_id"],
                                rec["session_id"],
                                rec.get("turn_id", ""),
                                rec["role"],
                                rec["content"],
                                rec.get("emotion", "neutral"),
                                rec.get("desktop_context_summary", ""),
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
                                emotion, desktop_context_summary, created_at, archived_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                rec["msg_id"],
                                rec["session_id"],
                                rec.get("turn_id", ""),
                                rec["role"],
                                rec["content"],
                                rec.get("emotion", "neutral"),
                                rec.get("desktop_context_summary", ""),
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


def _row_to_message(row: sqlite3.Row) -> MessageRecord:
    return MessageRecord(
        msg_id=int(row["msg_id"]),
        session_id=str(row["session_id"]),
        turn_id=str(row["turn_id"] or ""),
        role=str(row["role"]),
        content=str(row["content"]),
        emotion=str(row["emotion"] or "neutral"),
        desktop_context_summary=str(row["desktop_context_summary"] or ""),
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
