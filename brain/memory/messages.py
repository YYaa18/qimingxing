from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from brain.memory.models import MessageRecord, SessionRecord
from brain.memory.rows import _parse_json_list, _row_to_message


class MessagesMixin:
    """Session message CRUD + archive (moved verbatim from SQLiteMemoryStore)."""

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
        cards: Optional[List[Dict[str, Any]]] = None,
    ) -> MessageRecord:
        async with self._lock:
            conn = self._ensure_conn()
            now = time.time()
            sources = sources or []
            media = media or []
            cards = cards or []
            cur = conn.execute(
                """
                INSERT INTO messages (
                    session_id, turn_id, role, content, emotion,
                    desktop_context_summary, sources_json, media_json, cards_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, turn_id, role, content, emotion,
                    desktop_context_summary,
                    json.dumps(sources, ensure_ascii=False),
                    json.dumps(media, ensure_ascii=False),
                    json.dumps(cards, ensure_ascii=False),
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
                cards=cards,
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
                    cards_json, created_at, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        r["cards_json"] if "cards_json" in r.keys() else "[]",
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

