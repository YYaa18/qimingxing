from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from brain.config import MemoryExtractionConfig
from brain.context_store import ContextStore
from brain.context_builder import estimate_tokens
from brain.llm_router import LLMRouter
from brain.memory.backend import MemoryBackend
from brain.memory.embeddings import EmbeddingProvider, NullEmbeddingProvider, pack_embedding
from brain.memory.models import (
    CategoryTier,
    MemoryCategory,
    MemoryEpisode,
    MemoryFact,
    MemoryMutation,
    MessageRecord,
    new_episode_id,
    new_fact_id,
    normalize_category_key,
)

logger = logging.getLogger("qimingxing.memory.extractor")

MAX_DYNAMIC_CATEGORIES = 25

_EXPLICIT_CMD_RE = re.compile(
    r"(记住|帮我记|记一下|以后回答|以后回复|忘掉|忘记|删掉关于|删除关于|别再提|新建分类|建一个分类|建个分类|remember that|forget )"
)

_EXTRACTION_SYSTEM_PROMPT = """你是一个严谨的长期记忆分类、提炼与冲突消解器。你的任务是将对话按当前动态分类表进行分类整理，并提取值得留存的长期事实、工作情景或日常趣事/闲聊，同时与已有长期事实做冲突消解。

请严格只输出一个合法 JSON 对象，格式如下（不要输出任何 Markdown 代码块包裹外的解释文字）：
{
  "new_categories": [
    {
      "category_key": "英文小写下划线标识如 fitness_log",
      "display_name": "中文展示名如 健身记录",
      "description": "一句话描述适用内容",
      "tier": "core | episodic | chitchat",
      "inject_to_l1": false
    }
  ],
  "mutations": [
    {
      "op": "ADD | UPDATE | DELETE | NONE",
      "category": "优先使用【当前已有记忆分类】中的 category_key，或上面刚创建的 key",
      "content": "清晰完整的一句长期事实描述",
      "importance": 0.8,
      "target_fact_id": "仅当 op 为 UPDATE 或 DELETE 时填入对应的已有 fact_id，否则为 null"
    }
  ],
  "episode_summary": "如果本段对话包含值得跨天记忆的关键工作复盘/重要事件，用1句话总结；否则为 null",
  "chitchat_summary": "如果本段对话包含有记忆价值的日常闲聊/讲过的笑话/吐槽/生活趣事（非纯‘你好/谢谢/哦’），用1句话概括；否则为 null"
}

规则：
1. **分类优先复用（严禁同义重复）**：必须优先复用【当前已有记忆分类】中的 `category_key`（例如已有 `preference`/`tech_stack` 时禁止再建 `coding_prefs`/`my_tools` 等同义近义分类）。只有当用户明确要求“新建分类”，或出现持续性全新主题且现有分类无法覆盖时，才可在 `new_categories` 中注册（每次最多 2 个），且 `inject_to_l1` 必须保持 `false`，严禁为单次闲聊乱建分类。
2. **闲聊分级沉淀（严格区分纯寒暄与趣事）**：纯礼貌应答或单句寒暄（如“你好”“谢谢”“好的”“收到”“嗯”）必须输出 `op: "NONE"` 且 `chitchat_summary: null`；只有包含具体笑话、段子、生活趣事、吐槽或情感瞬间时，才总结写入 `chitchat_summary`（平时不干扰正事，被问到时能精准捞出）。
3. **冲突消解**：当新对话修正或升级了已有事实（如 Python 版本升级、项目变更、偏好改变），**必须**使用 `op: "UPDATE"` 并准确填入旧事实的 `target_fact_id`，严禁重复 `ADD` 造成矛盾事实共存。
4. **主动遗忘**：当用户明确要求“忘掉/忘记/删除”某信息时，**必须**输出 `op: "DELETE"` 并匹配对应 `target_fact_id`。
5. 绝对禁止编造不存在的信息，也禁止写入任何隐私占位（如 `[隐私窗口已隐藏]`）。"""

_COMPRESS_SYSTEM_PROMPT = """请将下面的历史对话压缩合并成 800–1000 字的中文滚动摘要，硬上限 1600 字。
必须使用以下固定标题分段：
【已完成/已确认】
【关键文件与路径】
【技术决策与原因】
【待办/未解决问题】
【用户偏好与约束】

要求：
- 只保留用户提出的关键前提、已确认的技术方案/结论、精确文件路径、函数/类名、报错码与待办；
- 不粘贴大段历史代码；需要细节时由后续工具重新读取最新文件；
- 省略寒暄、重复内容和已被推翻的方案；
- 直接输出摘要正文，不要额外解释。"""


_SUMMARY_HEADINGS = [
    "【已完成/已确认】",
    "【关键文件与路径】",
    "【技术决策与原因】",
    "【待办/未解决问题】",
    "【用户偏好与约束】",
]


def _split_summary_sections(text: str) -> Dict[str, str]:
    found: List[tuple[int, str]] = []
    for heading in _SUMMARY_HEADINGS:
        idx = text.find(heading)
        if idx >= 0:
            found.append((idx, heading))
    found.sort()
    sections: Dict[str, str] = {heading: "" for heading in _SUMMARY_HEADINGS}
    for pos, (idx, heading) in enumerate(found):
        end = found[pos + 1][0] if pos + 1 < len(found) else len(text)
        sections[heading] = text[idx + len(heading):end].strip()
    if found:
        preamble = text[: found[0][0]].strip()
        if preamble:
            sections[found[0][1]] = (preamble + "\n" + sections[found[0][1]]).strip()
    return sections


def _clip_structured_summary(new_summary: str, previous_summary: str, max_chars: int) -> str:
    """Keep all five headings; backfill missing sections; trim bodies only."""
    if not any(heading in new_summary for heading in _SUMMARY_HEADINGS):
        if len(new_summary) > max_chars:
            return new_summary[: max_chars - 3] + "…"
        return new_summary

    new_sections = _split_summary_sections(new_summary)
    old_sections = _split_summary_sections(previous_summary or "")
    for heading in _SUMMARY_HEADINGS:
        if not new_sections.get(heading) and old_sections.get(heading):
            new_sections[heading] = old_sections[heading]

    headings_len = sum(len(h) + 1 for h in _SUMMARY_HEADINGS)
    body_budget = max(0, max_chars - headings_len - 4 * 2)
    bodies = [new_sections.get(h, "") for h in _SUMMARY_HEADINGS]
    total_body = sum(len(b) for b in bodies)
    if total_body > body_budget:
        quota = max(40, body_budget // len(_SUMMARY_HEADINGS))
        trimmed: List[str] = []
        for body in bodies:
            if len(body) <= quota:
                trimmed.append(body)
            else:
                trimmed.append(body[: max(20, quota - 1)].rstrip() + "…")
        bodies = trimmed
        # If equal quotas overflowed due the ellipsis/minimum, trim tail bodies.
        while sum(len(b) for b in bodies) > body_budget:
            candidates = [i for i, b in enumerate(bodies) if len(b) > 24]
            if not candidates:
                break
            idx = max(candidates, key=lambda i: len(bodies[i]))
            bodies[idx] = bodies[idx][: max(20, len(bodies[idx]) - 8)].rstrip() + "…"

    rendered = "\n".join(
        f"{heading}\n{body}".rstrip()
        for heading, body in zip(_SUMMARY_HEADINGS, bodies)
    )
    if len(rendered) > max_chars:
        rendered = rendered[: max_chars - 1] + "…"
    return rendered


def has_explicit_memory_command(text: str) -> bool:
    if not text:
        return False
    return bool(_EXPLICIT_CMD_RE.search(text.lower()))


def parse_extraction_json(
    raw: str,
) -> tuple[List[MemoryCategory], List[MemoryMutation], Optional[str], Optional[str]]:
    """Parse dynamic new_categories, mutations, episode_summary, and chitchat_summary."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return [], [], None, None
    data = json.loads(cleaned[start : end + 1])

    new_categories: List[MemoryCategory] = []
    for item in (data.get("new_categories") or [])[:2]:
        key = normalize_category_key(str(item.get("category_key") or ""))
        if not key:
            continue
        raw_tier = str(item.get("tier") or "episodic").lower().strip()
        tier: CategoryTier = (
            raw_tier if raw_tier in ("core", "episodic", "chitchat") else "episodic"
        )  # type: ignore[assignment]
        # Protect L1 static prefix cache: LLM-created categories never auto-inject into L1
        inject_l1 = False
        default_w = 1.0 if tier == "core" else (0.55 if tier == "chitchat" else 0.90)
        default_d = 0.015 if tier == "core" else (0.15 if tier == "chitchat" else 0.04)
        new_categories.append(
            MemoryCategory(
                category_key=key,
                display_name=str(item.get("display_name") or key)[:40],
                description=str(item.get("description") or "")[:120],
                tier=tier,
                retrieval_weight=max(0.2, min(1.5, float(item.get("retrieval_weight", default_w)))),
                decay_rate=max(0.005, min(0.5, float(item.get("decay_rate", default_d)))),
                inject_to_l1=inject_l1,
                created_by="llm",
            )
        )

    mutations: List[MemoryMutation] = []
    for item in data.get("mutations") or []:
        op = str(item.get("op", "NONE")).upper().strip()
        if op not in ("ADD", "UPDATE", "DELETE", "NONE"):
            continue
        cat_key = normalize_category_key(str(item.get("category") or "fact")) or "fact"
        mutations.append(
            MemoryMutation(
                op=op,  # type: ignore[arg-type]
                category=cat_key,
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
    chitchat = data.get("chitchat_summary")
    chitchat_str = str(chitchat).strip() if chitchat else None
    return new_categories, mutations, ep_str, chitchat_str


class MemoryExtractor:
    """Async background extractor: explicit commands, throttled batching, conflict resolution, rolling compression, and sanitized desktop footprints."""

    def __init__(
        self,
        backend: MemoryBackend,
        llm_router: LLMRouter,
        embedding_provider: Optional[EmbeddingProvider] = None,
        config: Optional[MemoryExtractionConfig] = None,
        *,
        hot_message_limit: int = 10000,
        archive_batch_size: int = 2000,
    ) -> None:
        self.backend = backend
        self.llm_router = llm_router
        self.embedding_provider = embedding_provider or NullEmbeddingProvider()
        self.config = config or MemoryExtractionConfig()
        self.hot_message_limit = hot_message_limit
        self.archive_batch_size = archive_batch_size

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
        recent_budget: int = 0,
    ) -> None:
        """Non-blocking hook invoked after each completed turn.

        Fact/episode extraction is optional; rolling compression is context
        infrastructure and runs independently when compress_enabled is true.
        """
        if self.config.enabled:
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

        if self.config.compress_enabled:
            self._spawn(self._safe_compress(session_id, recent_budget))

        # Periodically persist sanitized desktop footprints (e.g. every 30 min)
        if (
            self.config.persist_desktop_footprints
            and time.time() - self._last_footprint_ts >= 1800.0
        ):
            self._spawn(self.persist_desktop_footprint(context_store))

        # Trigger non-blocking hot/cold message archiving when hot count exceeds limit
        self._spawn(self._safe_archive())

    def schedule_session_archive(
        self,
        *,
        session_id: str,
        context_store: ContextStore,
    ) -> None:
        """Flush buffered messages, sanitized desktop footprint & cold archive when archiving a session."""
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
        self._spawn(self._safe_archive())

    async def _safe_archive(self) -> None:
        async with self._get_lock("__archive__"):
            try:
                moved = await self.backend.archive_old_messages(
                    keep_latest=self.hot_message_limit,
                    batch_size=self.archive_batch_size,
                )
                if moved > 0:
                    logger.info("Archived %d cold messages into messages_archive", moved)
            except Exception as exc:
                logger.warning("Background hot/cold archive check failed (isolated): %s", exc)

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

    async def _safe_compress(self, session_id: str, recent_budget: int = 0) -> None:
        async with self._get_lock(f"compress:{session_id}"):
            try:
                await self.maybe_compress_session(session_id, recent_budget=recent_budget)
            except Exception as exc:
                logger.warning("Background session compression failed (isolated): %s", exc)

    async def _call_fast_llm(
        self,
        messages: List[Dict[str, str]],
        *,
        json_mode: bool = False,
    ) -> str:
        if getattr(self.llm_router._config, "has_explicit_routing", False):
            profile_name = "@slot:fast_utility"
        else:
            profile_name = self.config.profile
            if profile_name not in self.llm_router._config.profiles:
                profile_name = self.llm_router.active_profile
        chunks: List[str] = []
        async for tok in self.llm_router.stream_chat(
            messages, profile_name=profile_name, json_mode=json_mode
        ):
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
        categories = await self.backend.list_categories()
        cat_keys = {c.category_key for c in categories}
        cat_block = "\n".join(
            f"- {c.category_key}（{c.display_name} / tier={c.tier}）: {c.description}"
            for c in categories
        )
        facts_block = (
            "\n".join(
                f"- id={f.fact_id} [{f.category}] {f.content}" for f in existing_facts
            )
            if existing_facts
            else "（暂无已有长期事实）"
        )
        convo_block = "\n".join(f"{m.role}: {m.content}" for m in msgs)

        prompt = (
            f"【当前已有记忆分类】\n{cat_block}\n\n"
            f"【已有活跃长期事实】\n{facts_block}\n\n"
            f"【本批新增对话】\n{convo_block}\n\n"
            "请判断需要新建哪些分类（如有）、执行哪些记忆变更（ADD / UPDATE / DELETE / NONE），以及是否需要记录工作情景或日常趣事。"
        )
        raw = await self._call_fast_llm(
            [
                {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            json_mode=True,
        )
        new_categories, mutations, episode_summary, chitchat_summary = parse_extraction_json(raw)

        for cat in new_categories:
            if cat.category_key not in cat_keys:
                if len(cat_keys) >= MAX_DYNAMIC_CATEGORIES:
                    logger.warning(
                        "Dynamic category cap (%d) reached; skipping LLM-proposed category key=%s",
                        MAX_DYNAMIC_CATEGORIES,
                        cat.category_key,
                    )
                    continue
                await self.backend.register_category(cat)
                cat_keys.add(cat.category_key)
                logger.info(
                    "Dynamically registered new memory category key=%s (%s, tier=%s)",
                    cat.category_key,
                    cat.display_name,
                    cat.tier,
                )

        valid_ids = {f.fact_id for f in existing_facts}
        now = time.time()

        applied: List[MemoryMutation] = []
        for mut in mutations:
            if mut.op == "NONE":
                continue
            target_cat = mut.category if mut.category in cat_keys else "fact"
            if mut.op == "DELETE":
                if mut.target_fact_id and mut.target_fact_id in valid_ids:
                    await self.backend.update_fact_status(mut.target_fact_id, "deleted")
                    applied.append(mut)
                elif mut.content:
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
                        category=target_cat,
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
                if any(f.content == mut.content for f in existing_facts):
                    continue
                fact = MemoryFact(
                    fact_id=new_fact_id(),
                    category=target_cat,
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

        if chitchat_summary:
            ep_chat = MemoryEpisode(
                episode_id=new_episode_id(),
                kind="chitchat",
                summary=chitchat_summary,
                keywords="chitchat",
                importance=0.45,
                time_start=msgs[0].created_at,
                time_end=msgs[-1].created_at,
            )
            await self.backend.add_episode(ep_chat)
            await self._maybe_embed_episode(ep_chat)

        logger.info(
            "Memory extraction completed for session=%s applied=%d episode=%s chitchat=%s",
            session_id,
            len(applied),
            bool(episode_summary),
            bool(chitchat_summary),
        )
        return applied

    def cancel_idle_timers(self) -> None:
        """Cancel pending idle timers immediately on shutdown so lifespan exits without waiting."""
        for task in list(self._idle_tasks.values()):
            if not task.done():
                task.cancel()
        self._idle_tasks.clear()

    async def maybe_compress_session(
        self,
        session_id: str,
        *,
        recent_budget: int = 0,
    ) -> bool:
        """Compress until unsummarized tokens fall below the low watermark.

        High/low hysteresis keeps the summary (and therefore the L2 prefix)
        stable for multiple turns, improving prefix-cache hit rate.
        """
        if recent_budget <= 0:
            # Legacy callers/tests may not supply a dynamic recent budget;
            # retain the old turn-count behavior in that case.
            return await self._compress_legacy(session_id)

        effective_recent = max(512, int(recent_budget))
        high = max(512, int(effective_recent * max(0.2, self.config.compress_high_ratio)))
        low = max(256, int(effective_recent * min(0.95, max(0.1, self.config.compress_low_ratio))))
        batch_msgs = max(2, self.config.compress_batch_turns * 2)

        sess = await self.backend.get_session(session_id)
        if sess is None:
            return False

        all_msgs = await self.backend.get_messages(
            session_id,
            after_msg_id=sess.last_summarized_msg_id,
            limit=1000,
            oldest_first=True,
        )
        if not all_msgs:
            return False

        def msg_tokens(m: MessageRecord) -> int:
            return estimate_tokens(m.content)

        total_tokens = sum(msg_tokens(m) for m in all_msgs)
        if total_tokens < high:
            return False

        compressed_any = False
        current_summary = sess.rolling_summary or ""
        watermark = sess.last_summarized_msg_id
        pending = list(all_msgs)

        for _ in range(max(1, self.config.compress_max_batches)):
            if total_tokens <= low or not pending:
                break
            candidates = [m for m in pending if m.msg_id > watermark][:batch_msgs]
            if not candidates:
                break

            convo = "\n".join(f"{m.role}: {m.content}" for m in candidates)
            prompt = (
                f"【已有早期摘要】\n{current_summary or '（无）'}\n\n"
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
                break
            max_chars = max(400, int(self.config.compress_summary_max_chars))
            new_summary = _clip_structured_summary(
                new_summary, current_summary, max_chars
            )

            new_watermark = candidates[-1].msg_id
            await self.backend.update_session_summary(session_id, new_summary, new_watermark)
            logger.info(
                "Compressed session=%s up to msg_id=%d (len=%d, remaining_tokens=%d)",
                session_id,
                new_watermark,
                len(new_summary),
                total_tokens,
            )
            current_summary = new_summary
            watermark = new_watermark
            removed = {m.msg_id for m in candidates}
            pending = [m for m in pending if m.msg_id not in removed]
            total_tokens -= sum(msg_tokens(m) for m in candidates)
            compressed_any = True

        return compressed_any

    async def _compress_legacy(self, session_id: str) -> bool:
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
        max_chars = max(400, int(self.config.compress_summary_max_chars))
        new_summary = _clip_structured_summary(
            new_summary, sess.rolling_summary or "", max_chars
        )
        await self.backend.update_session_summary(
            session_id, new_summary, candidates[-1].msg_id
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
