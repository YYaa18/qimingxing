from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from brain.memory.models import (
    BackgroundTaskRecord,
    MemoryCategory,
    MemoryEpisode,
    MemoryFact,
    MemorySkill,
    MessageRecord,
    ScheduledTaskRecord,
    SessionRecord,
    WorkHistoryRecord,
)

def _row_to_session(row: Any) -> SessionRecord:
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


def _row_to_message(row: Any) -> MessageRecord:
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
        cards=_parse_json_list(row["cards_json"] if "cards_json" in row.keys() else "[]"),
        created_at=float(row["created_at"]),
    )


def _row_to_fact(row: Any) -> MemoryFact:
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


def _row_to_skill(row: Any) -> MemorySkill:
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


def _row_to_work_history(row: Any) -> WorkHistoryRecord:
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


def _row_to_scheduled_task(row: Any) -> ScheduledTaskRecord:
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


def _row_to_background_task(row: Any) -> BackgroundTaskRecord:
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


def _row_to_category(row: Any) -> MemoryCategory:
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


def _row_to_episode(row: Any) -> MemoryEpisode:
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


def _row_to_skill(row: Any) -> MemorySkill:
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


def _row_to_work_history(row: Any) -> WorkHistoryRecord:
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


def _row_to_scheduled_task(row: Any) -> ScheduledTaskRecord:
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


def _row_to_background_task(row: Any) -> BackgroundTaskRecord:
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
