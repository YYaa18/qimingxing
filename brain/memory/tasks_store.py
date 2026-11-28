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


class TasksStoreMixin:
    """Moved verbatim from SQLiteMemoryStore."""

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

