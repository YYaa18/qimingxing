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


class SkillsUsageMixin:
    """Moved verbatim from SQLiteMemoryStore."""

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

