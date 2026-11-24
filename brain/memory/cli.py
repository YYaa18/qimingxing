from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

from brain.config import load_config
from brain.eval_runner import format_eval_report, run_golden_evals
from brain.memory import MemoryCategory, create_memory_backend, normalize_category_key
from brain.memory.backend import MemoryBackend
from brain.memory.embeddings import create_embedding_provider, pack_embedding
from brain.memory.retrieval import MemoryRetriever
from brain.tools.mcp_client import MCPClientManager
from brain.tools.registry import ToolRegistry


async def cmd_list(backend: MemoryBackend, item_type: str, status: Optional[str], limit: int) -> None:
    if item_type == "facts":
        facts = await backend.list_facts(status=None if status == "all" else status, limit=limit)  # type: ignore[arg-type]
        for f in facts:
            print(
                f"{f.fact_id} | {f.status} | [{f.category}] imp={f.importance:.2f} "
                f"| emb={f.embedding_model or 'none'} | {f.content}"
            )
    elif item_type == "episodes":
        eps = await backend.list_episodes(status=None if status == "all" else status, limit=limit)  # type: ignore[arg-type]
        for ep in eps:
            print(
                f"{ep.episode_id} | {ep.status} | {ep.kind} | imp={ep.importance:.2f} "
                f"| emb={ep.embedding_model or 'none'} | {ep.summary}"
            )
    elif item_type == "sessions":
        sessions = await backend.list_sessions(limit=limit)
        for s in sessions:
            print(
                f"{s.session_id} | {s.status} | watermark={s.last_summarized_msg_id} "
                f"| summary={s.rolling_summary[:60]!r}"
            )


async def cmd_search(
    backend: MemoryBackend,
    retriever: MemoryRetriever,
    query: str,
    limit: int,
) -> None:
    results, elapsed_ms = await retriever.search(query, top_k=limit)
    print(f"Found {len(results)} result(s) in {elapsed_ms:.2f} ms:")
    for r in results:
        print(f"- {r.item_id} ({r.item_type}/{r.category}) score={r.score:.4f} | {r.content}")


async def cmd_inspect(backend: MemoryBackend, item_id: str) -> int:
    if item_id.startswith("fact_"):
        f = await backend.get_fact(item_id)
        if f is None:
            print(f"Not found: {item_id}", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "fact_id": f.fact_id,
                    "category": f.category,
                    "content": f.content,
                    "importance": f.importance,
                    "confidence": f.confidence,
                    "status": f.status,
                    "source_session_id": f.source_session_id,
                    "created_at": f.created_at,
                    "updated_at": f.updated_at,
                    "last_accessed_at": f.last_accessed_at,
                    "has_embedding": f.embedding is not None,
                    "embedding_model": f.embedding_model,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if item_id.startswith("ep_"):
        ep = await backend.get_episode(item_id)
        if ep is None:
            print(f"Not found: {item_id}", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "episode_id": ep.episode_id,
                    "kind": ep.kind,
                    "summary": ep.summary,
                    "keywords": ep.keywords,
                    "importance": ep.importance,
                    "status": ep.status,
                    "has_embedding": ep.embedding is not None,
                    "embedding_model": ep.embedding_model,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    sess = await backend.get_session(item_id)
    if sess is None:
        print(f"Not found: {item_id}", file=sys.stderr)
        return 1
    msgs = await backend.get_messages(sess.session_id, limit=100)
    print(
        json.dumps(
            {
                "session_id": sess.session_id,
                "status": sess.status,
                "rolling_summary": sess.rolling_summary,
                "message_count": len(msgs),
                "messages": [
                    {"msg_id": m.msg_id, "role": m.role, "content": m.content} for m in msgs
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


async def cmd_delete(backend: MemoryBackend, item_id: str) -> int:
    if item_id.startswith("fact_"):
        ok = await backend.update_fact_status(item_id, "deleted")
    elif item_id.startswith("ep_"):
        ok = await backend.update_episode_status(item_id, "deleted")
    else:
        ok = False
    if not ok:
        print(f"Not found or unsupported ID: {item_id}", file=sys.stderr)
        return 1
    print(f"Soft-deleted {item_id}")
    return 0


async def cmd_reindex(backend: MemoryBackend, config_path: Optional[Path]) -> int:
    cfg = load_config(config_path)
    provider = create_embedding_provider(cfg.memory.embedding)
    if not provider.enabled:
        print("Embedding provider is 'null' (disabled). Configure memory.embedding first.", file=sys.stderr)
        return 1

    facts = await backend.list_facts(status="active", limit=10000)
    episodes = await backend.list_episodes(status="active", limit=10000)
    total = 0

    for f in facts:
        if f.embedding is None or f.embedding_model != provider.model_name:
            vecs = await provider.embed_texts([f.content])
            if vecs and vecs[0]:
                await backend.update_fact_embedding(
                    f.fact_id, pack_embedding(vecs[0]), provider.model_name
                )
                total += 1

    for ep in episodes:
        if ep.embedding is None or ep.embedding_model != provider.model_name:
            vecs = await provider.embed_texts([ep.summary])
            if vecs and vecs[0]:
                await backend.update_episode_embedding(
                    ep.episode_id, pack_embedding(vecs[0]), provider.model_name
                )
                total += 1

    print(f"Reindexed {total} active memory records with model={provider.model_name}")
    return 0


async def run_cli(args: argparse.Namespace) -> int:
    if args.command == "eval":
        cases_path = Path(args.cases) if args.cases else None
        report = await run_golden_evals(cases_path)
        if args.json:
            print(
                json.dumps(
                    {
                        "total": report.total,
                        "applicable": report.applicable,
                        "passed": report.passed,
                        "partial": report.partial,
                        "failed": report.failed,
                        "not_applicable": report.not_applicable,
                        "score_pct": report.score_pct,
                        "results": [
                            {
                                "id": r.case_id,
                                "category": r.category,
                                "title": r.title,
                                "status": r.status,
                                "score": r.score,
                                "detail": r.detail,
                                "elapsed_ms": r.elapsed_ms,
                            }
                            for r in report.results
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(format_eval_report(report))
        return 0 if report.failed == 0 else 1

    cfg = load_config(args.config)
    if args.db:
        cfg.memory.sqlite_path = str(args.db)
    backend = create_memory_backend(cfg.memory)
    await backend.initialize()
    try:
        if args.command == "list":
            await cmd_list(backend, args.type, args.status, args.limit)
            return 0
        if args.command == "search":
            provider = create_embedding_provider(cfg.memory.embedding)
            retriever = MemoryRetriever(
                backend, provider, timeout_ms=cfg.memory.embedding.timeout_ms
            )
            await cmd_search(backend, retriever, args.query, args.limit)
            return 0
        if args.command == "inspect":
            return await cmd_inspect(backend, args.id)
        if args.command == "delete":
            return await cmd_delete(backend, args.id)
        if args.command == "export":
            count = await backend.export_snapshot(args.path)
            print(f"Exported {count} records to {args.path}")
            return 0
        if args.command == "import":
            count = await backend.import_snapshot(args.path)
            print(f"Imported {count} records from {args.path}")
            return 0
        if args.command == "reindex":
            return await cmd_reindex(backend, args.config)
        if args.command == "categories":
            if args.add:
                key = normalize_category_key(args.add)
                if not key:
                    print("Invalid category key", file=sys.stderr)
                    return 1
                cat = MemoryCategory(
                    category_key=key,
                    display_name=args.name or key,
                    description=args.desc or "",
                    tier=args.tier,
                    retrieval_weight=args.weight,
                    decay_rate=args.decay,
                    inject_to_l1=args.l1,
                    created_by="cli",
                )
                await backend.register_category(cat)
                print(f"Registered category: {cat.category_key} ({cat.display_name}, tier={cat.tier})")
                return 0
            cats = await backend.list_categories()
            for c in cats:
                print(
                    f"{c.category_key} | {c.display_name} | tier={c.tier} | "
                    f"w={c.retrieval_weight:.2f} | decay={c.decay_rate:.3f} | "
                    f"l1={c.inject_to_l1} | by={c.created_by}"
                )
            return 0
        if args.command == "archive":
            hot_before, cold_before = await backend.count_messages()
            moved = await backend.archive_old_messages(
                keep_latest=args.keep, batch_size=args.batch
            )
            hot_after, cold_after = await backend.count_messages()
            print(
                f"Archived {moved} message(s): hot={hot_before}->{hot_after}, "
                f"archived={cold_before}->{cold_after}"
            )
            return 0
        if args.command == "skills":
            subcmd = args.skills_action
            if subcmd == "list":
                rows = await backend.list_skills(
                    status=None if args.status == "all" else args.status,
                    limit=args.limit,
                )
                if not rows:
                    print("No skills stored.")
                for s in rows:
                    print(
                        f"{s.skill_id} | {s.status} | {s.tool_preference} | "
                        f"ok={s.success_count} fail={s.failure_count} | {s.name} — {s.trigger_intent}"
                    )
                return 0
            if subcmd == "inspect":
                if not args.id:
                    print("Please specify skill_id with --id", file=sys.stderr)
                    return 1
                sk = await backend.get_skill(args.id)
                if sk is None:
                    print(f"Skill not found: {args.id}", file=sys.stderr)
                    return 1
                print(
                    json.dumps(
                        {
                            "skill_id": sk.skill_id,
                            "name": sk.name,
                            "trigger_intent": sk.trigger_intent,
                            "tool_preference": sk.tool_preference,
                            "prerequisites": sk.prerequisites,
                            "steps": json.loads(sk.steps_json),
                            "success_count": sk.success_count,
                            "failure_count": sk.failure_count,
                            "status": sk.status,
                            "updated_at": sk.updated_at,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            if subcmd == "delete":
                if not args.id:
                    print("Please specify skill_id with --id", file=sys.stderr)
                    return 1
                ok = await backend.update_skill_status(args.id, "deprecated")
                if not ok:
                    print(f"Skill not found: {args.id}", file=sys.stderr)
                    return 1
                print(f"Deprecated skill {args.id}")
                return 0
            return 1
        if args.command == "history":
            if args.id:
                rec = await backend.get_work_history(args.id)
                if rec is None:
                    print(f"Work history not found: {args.id}", file=sys.stderr)
                    return 1
                print(
                    json.dumps(
                        {
                            "work_id": rec.work_id,
                            "session_id": rec.session_id,
                            "turn_id": rec.turn_id,
                            "goal": rec.goal,
                            "result_summary": rec.result_summary,
                            "step_count": rec.step_count,
                            "status": rec.status,
                            "created_at": rec.created_at,
                            "trace": json.loads(rec.trace_json),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            rows = await backend.list_work_history(limit=args.limit)
            if not rows:
                print("No work history recorded.")
            for w in rows:
                print(
                    f"{w.work_id} | {w.status} | steps={w.step_count} | {w.goal} -> {w.result_summary[:80]}"
                )
            return 0
        if args.command == "usage":
            since = 0.0 if args.days <= 0 else time.time() - args.days * 86400.0
            summary = await backend.get_usage_summary(since_ts=since)
            scope = f"last {args.days}d" if args.days > 0 else "all-time"
            print(
                f"Usage ({scope}): calls={summary.calls} | "
                f"prompt={summary.prompt_tokens} (cache_hit={summary.prompt_cache_hit_tokens}, "
                f"hit_rate={summary.cache_hit_rate * 100:.1f}%) | "
                f"completion={summary.completion_tokens} | "
                f"est_cost_cny=¥{summary.total_cost_cny:.4f}"
            )
            return 0
        if args.command == "tasks":
            if args.cancel:
                updated = await backend.update_scheduled_task(args.cancel, status="cancelled")
                if updated is None:
                    print(f"Task not found: {args.cancel}", file=sys.stderr)
                    return 1
                print(f"Cancelled task {updated.task_id} ({updated.title})")
                return 0
            tasks = await backend.list_scheduled_tasks(
                status=None if args.status == "all" else args.status,
                limit=args.limit,
            )
            if not tasks:
                print("No scheduled tasks stored.")
            for t in tasks:
                print(
                    f"{t.task_id} | {t.status} | {t.task_type} | {t.schedule_kind}({t.schedule_expr}) | {t.title}"
                )
            return 0
        if args.command == "mcp":
            if not cfg.mcp_servers:
                print("No MCP servers configured.")
                return 0
            if args.probe:
                reg = ToolRegistry()
                mgr = MCPClientManager(cfg)
                try:
                    statuses = await mgr.start_all(reg)
                    for st in statuses:
                        tools_str = ", ".join(st.tools) if st.tools else "-"
                        err_str = f" | err={st.error}" if st.error else ""
                        print(f"{st.name} | {st.transport} | {st.status} | tools=[{tools_str}]{err_str}")
                finally:
                    await mgr.stop_all(reg)
                return 0
            for srv in cfg.mcp_servers:
                state = "enabled" if srv.enabled else "disabled"
                target = srv.command if srv.transport == "stdio" else srv.url
                print(
                    f"{srv.name} | {srv.transport} | {state} | safe={srv.safe_tools} | target={target}"
                )
            return 0
        return 1
    finally:
        await backend.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Qimingxing Long-Term Memory CLI")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    parser.add_argument("--db", type=Path, default=None, help="Override SQLite DB path")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List stored facts, episodes, or sessions")
    p_list.add_argument("--type", choices=["facts", "episodes", "sessions"], default="facts")
    p_list.add_argument("--status", choices=["active", "superseded", "deleted", "all"], default="active")
    p_list.add_argument("--limit", type=int, default=50)

    p_search = sub.add_parser("search", help="Search long-term memory")
    p_search.add_argument("query", help="Search query text")
    p_search.add_argument("--limit", type=int, default=5)

    p_inspect = sub.add_parser("inspect", help="Inspect a fact, episode, or session by ID")
    p_inspect.add_argument("id")

    p_delete = sub.add_parser("delete", help="Soft-delete a fact or episode by ID")
    p_delete.add_argument("id")

    p_export = sub.add_parser("export", help="Export memory snapshot to JSONL")
    p_export.add_argument("path", type=Path)

    p_import = sub.add_parser("import", help="Import memory snapshot from JSONL")
    p_import.add_argument("path", type=Path)

    sub.add_parser("reindex", help="Backfill embeddings for active records")

    p_cats = sub.add_parser("categories", help="List or register dynamic memory categories")
    p_cats.add_argument("--add", default=None, help="Category key to register/update (e.g. fitness_log)")
    p_cats.add_argument("--name", default=None, help="Display name (e.g. 健身记录)")
    p_cats.add_argument("--desc", default="", help="Category description")
    p_cats.add_argument("--tier", choices=["core", "episodic", "chitchat"], default="episodic")
    p_cats.add_argument("--weight", type=float, default=0.90)
    p_cats.add_argument("--decay", type=float, default=0.04)
    p_cats.add_argument("--l1", action="store_true", help="Inject into L1 static profile")

    p_arch = sub.add_parser("archive", help="Archive old hot messages into messages_archive")
    p_arch.add_argument("--keep", type=int, default=10000, help="Hot message retention count")
    p_arch.add_argument("--batch", type=int, default=2000, help="Max messages to move per run")

    p_skills = sub.add_parser("skills", help="Manage learned procedural skills")
    p_skills.add_argument("skills_action", nargs="?", choices=["list", "inspect", "delete"], default="list")
    p_skills.add_argument("--id", default=None, help="Skill ID for inspect/delete")
    p_skills.add_argument("--status", choices=["active", "deprecated", "all"], default="active")
    p_skills.add_argument("--limit", type=int, default=50)

    p_hist = sub.add_parser("history", help="List or inspect archived multi-step work history")
    p_hist.add_argument("--id", default=None, help="Work ID to inspect full trace JSON")
    p_hist.add_argument("--limit", type=int, default=20)

    p_usage = sub.add_parser("usage", help="Show LLM usage ledger summary and cache hit rate")
    p_usage.add_argument("--days", type=int, default=1, help="Window in days (0 = all time)")

    p_tasks = sub.add_parser("tasks", help="List or cancel scheduled tasks")
    p_tasks.add_argument("--status", choices=["active", "completed", "cancelled", "all"], default="all")
    p_tasks.add_argument("--cancel", default=None, help="Cancel scheduled task by ID")
    p_tasks.add_argument("--limit", type=int, default=50)

    p_mcp = sub.add_parser("mcp", help="Inspect configured MCP servers and optionally probe live tools")
    p_mcp.add_argument("--probe", action="store_true", help="Connect to enabled MCP servers and list discovered tools")

    p_eval = sub.add_parser("eval", help="Run personal golden regression evaluation suite")
    p_eval.add_argument("--cases", default=None, help="Path to golden_cases.yaml")
    p_eval.add_argument("--json", action="store_true", help="Output JSON report")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
