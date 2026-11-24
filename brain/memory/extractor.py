from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from brain.config import MemoryExtractionConfig
from brain.context_store import ContextStore
from brain.llm_router import LLMRouter
from brain.memory.backend import MemoryBackend
from brain.memory.embeddings import EmbeddingProvider, NullEmbeddingProvider, pack_embedding
from brain.memory.models import (
    MemoryEpisode,
    MemoryFact,
    MemoryMutation,
    MessageRecord,
    new_episode_id,
    new_fact_id,
)

logger = logging.getLogger("qimingxing.memory.extractor")

_EXPLICIT_CMD_RE = re.compile(
    r"(记住|帮我记|记一下|以后回答|以后回复|忘掉|忘记|删掉关于|删除关于|别再提|remember that|forget )"
)

_EXTRACTION_SYSTEM_PROMPT = """你是一个严谨的长期记忆提炼与冲突消解器。你的任务是从对话片段中提取有长期价值的事实（偏好、技术栈、正在做的项目、长期设定、习惯）或值得跨天记住的重要事件，并与已有长期事实做冲突消解。

请严格只输出一个合法 JSON 对象，格式如下（不要输出任何 Markdown 代码块包裹外的解释文字）：
{
  "mutations": [
    {
      "op": "ADD | UPDATE | DELETE | NONE",
      "category": "preference | project | tech_stack | habit | fact",
      "content": "清晰完整的一句长期事实描述",
      "importance": 0.8,
      "target_fact_id": "仅当 op 为 UPDATE 或 DELETE 时填入对应的已有 fact_id，否则为 null"
    }
  ],
  "episode_summary": "如果本段对话包含值得跨天记忆的关键里程碑/重要问题复盘，用1句话总结；否则为 null"
}

规则：
1. 如果只是普通寒暄（如“你好”、“谢谢”、“好的”）、一次性查询天气或无长期价值内容，只输出 `{"mutations": [{"op": "NONE"}], "episode_summary": null}`。
2. 当新对话修正或升级了已有事实（例如 Python 版本升级、项目变更、偏好改变），**必须**使用 `op: "UPDATE"` 并准确填入旧事实的 `target_fact_id`，严禁重复 `ADD` 造成矛盾事实共存。
3. 当用户明确要求“忘掉/忘记/删除”某信息时，**必须**输出 `op: "DELETE"` 并匹配对应 `target_fact_id`。
4. 绝对禁止编造不存在的信息，也禁止写入任何隐私占位（如 `[隐私窗口已隐藏]`）。"""

_COMPRESS_SYSTEM_PROMPT = """请将下面的历史对话压缩合并成不超过 220 字的简明中文滚动摘要。保留：用户提出的关键前提、已确定的技术方案/结论、待跟进事项；省略寒暄与重复细节。直接输出摘要正文。"""


def has_explicit_memory_command(text: str) -> bool:
    if not text:
        return False
    return bool(_EXPLICIT_CMD_RE.search(text.lower()))


def parse_extraction_json(raw: str) -> tuple[List[MemoryMutation], Optional[str]]:
    """Parse structured mutations + optional episode summary from LLM output."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    # Locate outer JSON object defensively
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return [], None
    data = json.loads(cleaned[start : end + 1])
    mutations: List[MemoryMutation] = []
    for item in data.get("mutations") or []:
        op = str(item.get("op", "NONE")).upper().strip()
        if op not in ("ADD", "UPDATE", "DELETE", "NONE"):
            continue
        mutations.append(
            MemoryMutation(
                op=op,  # type: ignore[arg-type]
                category=str(item.get("category") or "fact"),
                content=str(item.get("content") or "").strip(),
                importance=max(0.1, min(1.0, float(item.get("importance", 0.7)))),
                confidence=max(0.1, min(1.0, float(item.get("confidence", 0.9)))),
                target_fact_id=(
                    str(item["target_fact_id"]) if item.get("target_fact_id") else None
                ),
            )
        )
    ep = data.get("episode_summary")
    ep_str = str(ep).strip() if ep else None
    return mutations, ep_str


class MemoryExtractor:
    """Async background extractor: explicit commands, throttled batching, conflict resolution, rolling compression, and sanitized desktop footprints."""

    def __init__(
        self,
        backend: MemoryBackend,
        llm_router: LLMRouter,
        embedding_provider: Optional[EmbeddingProvider] = None,
        config: Optional[MemoryExtractionConfig] = None,
    ) -> None:
        self.backend = backend
        self.llm_router = llm_router
        self.embedding_provider = embedding_provider or NullEmbeddingProvider()
        self.config = config or MemoryExtractionConfig()

        self._pending_turns: Dict[str, int] = {}
        self._unextracted_msgs: Dict[str, List[MessageRecord]] = {}
        self._idle_tasks: Dict[str, asyncio.Task[None]] = {}
        self._last_footprint_ts: float = 0.0
        self._bg_tasks: set[asyncio.Task[None]] = set()
        self._session_locks: Dict[str, asyncio.Lock] = {}

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    def _spawn(self, coro: Any) -> asyncio.Task[None]:
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    async def wait_for_idle(self) -> None:
        """Await any currently running background extraction/compression tasks (helpful for tests & shutdown)."""
        for task in list(self._bg_tasks):
            try:
                await task
            except Exception:
                pass

    def schedule_post_turn(
        self,
        *,
        session_id: str,
        user_msg: MessageRecord,
        assistant_msg: Optional[MessageRecord],
        context_store: ContextStore,
    ) -> None:
        """Non-blocking hook invoked after each completed turn."""
        if not self.config.enabled:
            return

        buf = self._unextracted_msgs.setdefault(session_id, [])
        buf.append(user_msg)
        if assistant_msg is not None:
            buf.append(assistant_msg)
        self._pending_turns[session_id] = self._pending_turns.get(session_id, 0) + 1

        # Reset idle timer
        old_idle = self._idle_tasks.pop(session_id, None)
        if old_idle is not None and not old_idle.done():
            old_idle.cancel()

        explicit = has_explicit_memory_command(user_msg.content)
        batch_reached = self._pending_turns[session_id] >= max(1, self.config.batch_turns)

        if explicit or batch_reached:
            msgs = self._drain_buffer(session_id)
            self._spawn(self._safe_extract(session_id, msgs))
        else:
            self._idle_tasks[session_id] = self._spawn(self._idle_flush_after(session_id))

        # Trigger async rolling summary compression if unsummarized messages exceed threshold
        self._spawn(self._safe_compress(session_id))

        # Periodically persist sanitized desktop footprints (e.g. every 30 min)
        if (
            self.config.persist_desktop_footprints
            and time.time() - self._last_footprint_ts >= 1800.0
        ):
            self._spawn(self.persist_desktop_footprint(context_store))

    def schedule_session_archive(
        self,
        *,
        session_id: str,
        context_store: ContextStore,
    ) -> None:
        """Flush buffered messages & sanitized desktop footprint when archiving a session."""
        if not self.config.enabled:
            return
        old_idle = self._idle_tasks.pop(session_id, None)
        if old_idle is not None and not old_idle.done():
            old_idle.cancel()
        msgs = self._drain_buffer(session_id)
        if msgs:
            self._spawn(self._safe_extract(session_id, msgs))
        if self.config.persist_desktop_footprints:
            self._spawn(self.persist_desktop_footprint(context_store))

    def _drain_buffer(self, session_id: str) -> List[MessageRecord]:
        self._pending_turns[session_id] = 0
        return self._unextracted_msgs.pop(session_id, [])

    async def _idle_flush_after(self, session_id: str) -> None:
        try:
            await asyncio.sleep(max(1.0, self.config.idle_seconds))
            msgs = self._drain_buffer(session_id)
            if msgs:
                await self._safe_extract(session_id, msgs)
        except asyncio.CancelledError:
            return

    async def _safe_extract(self, session_id: str, msgs: List[MessageRecord]) -> None:
        if not msgs:
            return
        async with self._get_lock(session_id):
            try:
                await self.extract_from_messages(session_id, msgs)
            except Exception as exc:
                logger.warning("Background memory extraction failed (isolated): %s", exc)

    async def _safe_compress(self, session_id: str) -> None:
        async with self._get_lock(f"compress:{session_id}"):
            try:
                await self.maybe_compress_session(session_id)
            except Exception as exc:
                logger.warning("Background session compression failed (isolated): %s", exc)

    async def _call_fast_llm(self, messages: List[Dict[str, str]]) -> str:
        profile_name = self.config.profile
        if profile_name not in self.llm_router._config.profiles:
            profile_name = self.llm_router.active_profile
        chunks: List[str] = []
        async for tok in self.llm_router.stream_chat(messages, profile_name=profile_name):
            chunks.append(tok)
        return "".join(chunks)

    async def extract_from_messages(
        self,
        session_id: str,
        msgs: List[MessageRecord],
    ) -> List[MemoryMutation]:
        if not msgs:
            return []

        existing_facts = await self.backend.list_facts(status="active", limit=40)
        facts_block = (
            "\n".join(
                f"- id={f.fact_id} [{f.category}] {f.content}" for f in existing_facts
            )
            if existing_facts
            else "（暂无已有长期事实）"
        )
        convo_block = "\n".join(f"{m.role}: {m.content}" for m in msgs)

        prompt = (
            f"【已有活跃长期事实】\n{facts_block}\n\n"
            f"【本批新增对话】\n{convo_block}\n\n"
            "请判断需要执行哪些记忆变更（ADD / UPDATE / DELETE / NONE）。"
        )
        raw = await self._call_fast_llm(
            [
                {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
        )
        mutations, episode_summary = parse_extraction_json(raw)
        valid_ids = {f.fact_id for f in existing_facts}
        now = time.time()

        applied: List[MemoryMutation] = []
        for mut in mutations:
            if mut.op == "NONE":
                continue
            if mut.op == "DELETE":
                if mut.target_fact_id and mut.target_fact_id in valid_ids:
                    await self.backend.update_fact_status(mut.target_fact_id, "deleted")
                    applied.append(mut)
                elif mut.content:
                    # Fallback fuzzy match if model returned content without target_fact_id
                    for f in existing_facts:
                        if mut.content in f.content or f.content in mut.content:
                            await self.backend.update_fact_status(f.fact_id, "deleted")
                            applied.append(mut)
            elif mut.op == "UPDATE":
                if mut.target_fact_id and mut.target_fact_id in valid_ids:
                    await self.backend.update_fact_status(mut.target_fact_id, "superseded")
                if mut.content:
                    fact = MemoryFact(
                        fact_id=new_fact_id(),
                        category=mut.category,
                        content=mut.content,
                        importance=mut.importance,
                        confidence=mut.confidence,
                        status="active",
                        source_session_id=session_id,
                        created_at=now,
                        updated_at=now,
                        last_accessed_at=now,
                    )
                    await self.backend.add_fact(fact)
                    await self._maybe_embed_fact(fact)
                    applied.append(mut)
            elif mut.op == "ADD" and mut.content:
                # Avoid inserting exact duplicates
                if any(f.content == mut.content for f in existing_facts):
                    continue
                fact = MemoryFact(
                    fact_id=new_fact_id(),
                    category=mut.category,
                    content=mut.content,
                    importance=mut.importance,
                    confidence=mut.confidence,
                    status="active",
                    source_session_id=session_id,
                    created_at=now,
                    updated_at=now,
                    last_accessed_at=now,
                )
                await self.backend.add_fact(fact)
                await self._maybe_embed_fact(fact)
                applied.append(mut)

        if episode_summary:
            ep = MemoryEpisode(
                episode_id=new_episode_id(),
                kind="conversation",
                summary=episode_summary,
                keywords="",
                importance=0.65,
                time_start=msgs[0].created_at,
                time_end=msgs[-1].created_at,
            )
            await self.backend.add_episode(ep)
            await self._maybe_embed_episode(ep)

        logger.info(
            "Memory extraction completed for session=%s applied=%d episode=%s",
            session_id,
            len(applied),
            bool(episode_summary),
        )
        return applied

    def cancel_idle_timers(self) -> None:
        """Cancel pending idle timers immediately on shutdown so lifespan exits without waiting."""
        for task in list(self._idle_tasks.values()):
            if not task.done():
                task.cancel()
        self._idle_tasks.clear()

    async def maybe_compress_session(self, session_id: str) -> bool:
        trigger_msgs = max(2, self.config.compress_trigger_turns * 2)
        batch_msgs = max(2, self.config.compress_batch_turns * 2)

        unsummarized_count = await self.backend.count_unsummarized_messages(session_id)
        if unsummarized_count < trigger_msgs:
            return False

        sess = await self.backend.get_session(session_id)
        if sess is None:
            return False

        candidates = await self.backend.get_messages(
            session_id,
            after_msg_id=sess.last_summarized_msg_id,
            limit=batch_msgs,
            oldest_first=True,
        )
        if len(candidates) < batch_msgs:
            return False

        convo = "\n".join(f"{m.role}: {m.content}" for m in candidates)
        prompt = (
            f"【已有早期摘要】\n{sess.rolling_summary or '（无）'}\n\n"
            f"【待并入压缩的较早对话】\n{convo}"
        )
        new_summary = (
            await self._call_fast_llm(
                [
                    {"role": "system", "content": _COMPRESS_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ]
            )
        ).strip()
        if not new_summary:
            return False
        if len(new_summary) > 300:
            new_summary = new_summary[:297] + "…"

        new_watermark = candidates[-1].msg_id
        await self.backend.update_session_summary(session_id, new_summary, new_watermark)
        logger.info(
            "Compressed session=%s up to msg_id=%d (len=%d)",
            session_id,
            new_watermark,
            len(new_summary),
        )
        return True

    async def persist_desktop_footprint(self, context_store: ContextStore) -> Optional[MemoryEpisode]:
        """Persist sanitized non-blacklisted desktop activity timeline to L3 episodes."""
        entries = context_store.get_sanitized_timeline_for_footprint()
        if not entries:
            return None

        # Extra defensive check: ensure no privacy_masked or placeholder text ever passes
        safe_entries = [
            e
            for e in entries
            if not e.privacy_masked
            and not e.is_self_window
            and "[隐私窗口已隐藏]" not in e.app_name
            and "[隐私窗口已隐藏]" not in e.window_title
        ]
        if not safe_entries:
            return None

        apps: List[str] = []
        details: List[str] = []
        for e in safe_entries:
            if e.app_name not in apps:
                apps.append(e.app_name)
            details.append(e.format_line())

        summary = f"桌面工作足迹：先后使用 {', '.join(apps)}（" + "；".join(details[-5:]) + "）"
        ep = MemoryEpisode(
            episode_id=new_episode_id(),
            kind="desktop_footprint",
            summary=summary,
            keywords=" ".join(apps),
            importance=0.6,
            time_start=safe_entries[0].started_at,
            time_end=safe_entries[-1].ended_at,
        )
        await self.backend.add_episode(ep)
        await self._maybe_embed_episode(ep)
        self._last_footprint_ts = time.time()
        logger.info("Persisted sanitized desktop footprint episode=%s apps=%s", ep.episode_id, apps)
        return ep

    async def _maybe_embed_fact(self, fact: MemoryFact) -> None:
        if not self.embedding_provider.enabled:
            return
        try:
            vecs = await self.embedding_provider.embed_texts([fact.content])
            if vecs and vecs[0]:
                await self.backend.update_fact_embedding(
                    fact.fact_id,
                    pack_embedding(vecs[0]),
                    self.embedding_provider.model_name,
                )
        except Exception as exc:
            logger.debug("Skipping background fact embedding: %s", exc)

    async def _maybe_embed_episode(self, ep: MemoryEpisode) -> None:
        if not self.embedding_provider.enabled:
            return
        try:
            vecs = await self.embedding_provider.embed_texts([ep.summary])
            if vecs and vecs[0]:
                await self.backend.update_episode_embedding(
                    ep.episode_id,
                    pack_embedding(vecs[0]),
                    self.embedding_provider.model_name,
                )
        except Exception as exc:
            logger.debug("Skipping background episode embedding: %s", exc)
