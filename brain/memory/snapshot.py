from __future__ import annotations

import base64
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
from brain.memory.backend import SNAPSHOT_SCHEMA_VERSION
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


class SnapshotMixin:
    """Moved verbatim from SQLiteMemoryStore."""

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
                                media_json, cards_json, created_at
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
                                json.dumps(_snapshot_rich(rec, "cards"), ensure_ascii=False),
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
                                media_json, cards_json, created_at, archived_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                                json.dumps(_snapshot_rich(rec, "cards"), ensure_ascii=False),
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
