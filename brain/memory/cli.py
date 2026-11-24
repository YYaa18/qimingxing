from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

from brain.config import load_config
from brain.memory import create_memory_backend
from brain.memory.backend import MemoryBackend
from brain.memory.embeddings import create_embedding_provider, pack_embedding
from brain.memory.retrieval import MemoryRetriever


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
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
