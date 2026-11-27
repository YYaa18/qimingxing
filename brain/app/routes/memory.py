from __future__ import annotations

import time
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from brain.app.routes._deps import RouteDeps


def build_memory_router(*, deps: RouteDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/memory/debug")
    async def memory_debug(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in deps.loopback_hosts:
            raise HTTPException(
                status_code=403, detail="Memory debug inspection is restricted to localhost."
            )

        session = await deps.backend.get_or_create_active_session()
        facts = await deps.backend.list_facts(status="active", limit=50)
        episodes = await deps.backend.list_episodes(status="active", limit=20)
        recent_msgs = await deps.backend.get_messages(session.session_id, limit=12)
        categories = await deps.backend.list_categories()
        hot_count, archived_count = await deps.backend.count_messages()
        skills = await deps.backend.list_skills(status="active", limit=30)
        work_logs = await deps.backend.list_work_history(limit=10)
        scheduled_tasks = await deps.backend.list_scheduled_tasks(status="active", limit=100)
        lt = time.localtime()
        day_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        today_usage = await deps.backend.get_usage_summary(since_ts=day_start)

        return JSONResponse(
            {
                "active_session": {
                    "session_id": session.session_id,
                    "status": session.status,
                    "rolling_summary": session.rolling_summary,
                    "last_summarized_msg_id": session.last_summarized_msg_id,
                    "recent_message_count": len(recent_msgs),
                },
                "hot_messages_count": hot_count,
                "archived_messages_count": archived_count,
                "skills_count": len(skills),
                "work_history_count": len(work_logs),
                "scheduled_tasks_count": len(scheduled_tasks),
                "mcp_servers": [asdict(s) for s in deps.mcp.list_statuses()],
                "last_build_stats": (
                    asdict(deps.hub.last_build_stats)
                    if getattr(deps.hub, "last_build_stats", None) is not None
                    else None
                ),
                "today_usage": {
                    "calls": today_usage.calls,
                    "prompt_tokens": today_usage.prompt_tokens,
                    "cached_input_tokens": today_usage.cached_input_tokens,
                    "completion_tokens": today_usage.completion_tokens,
                    "total_cost_cny": round(today_usage.total_cost_cny, 6),
                    "cache_hit_rate": round(today_usage.cache_hit_rate, 4),
                },
                "categories": [
                    {
                        "category_key": c.category_key,
                        "display_name": c.display_name,
                        "tier": c.tier,
                        "retrieval_weight": c.retrieval_weight,
                        "decay_rate": c.decay_rate,
                        "inject_to_l1": c.inject_to_l1,
                        "created_by": c.created_by,
                    }
                    for c in categories
                ],
                "active_facts_count": len(facts),
                "active_facts": [
                    {
                        "fact_id": f.fact_id,
                        "category": f.category,
                        "content": f.content,
                        "importance": f.importance,
                        "embedding_model": f.embedding_model,
                    }
                    for f in facts
                ],
                "recent_episodes": [
                    {
                        "episode_id": ep.episode_id,
                        "kind": ep.kind,
                        "summary": ep.summary,
                        "embedding_model": ep.embedding_model,
                    }
                    for ep in episodes
                ],
                "last_context_stats": asdict(deps.hub.last_build_stats)
                if deps.hub.last_build_stats
                else None,
            }
        )

    return router
