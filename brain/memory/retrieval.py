from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from typing import Dict, List, Optional, Set, Tuple

from brain.memory.backend import MemoryBackend
from brain.memory.embeddings import (
    EmbeddingProvider,
    NullEmbeddingProvider,
    cosine_similarity,
    unpack_embedding,
)
from brain.memory.models import MemoryCategory, RetrievedMemory, tokenize_query_for_fts

logger = logging.getLogger("qimingxing.memory.retrieval")

_RRF_K = 60
_CHITCHAT_RECALL_RE = re.compile(
    r"(笑话|段子|闲聊|聊天|上次聊|之前聊|之前说|前几天|那个梗|聊过)"
)


def is_chitchat_recall_query(query: str) -> bool:
    return bool(query and _CHITCHAT_RECALL_RE.search(query))


class MemoryRetriever:
    """Fast local FTS5 + dynamic category weighting + recency + importance retriever with RRF slot."""

    def __init__(
        self,
        backend: MemoryBackend,
        embedding_provider: Optional[EmbeddingProvider] = None,
        *,
        timeout_ms: int = 800,
    ) -> None:
        self.backend = backend
        self.embedding_provider = embedding_provider or NullEmbeddingProvider()
        self.timeout_ms = timeout_ms

    async def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        exclude_fact_ids: Optional[Set[str]] = None,
    ) -> Tuple[List[RetrievedMemory], float]:
        started = time.perf_counter()
        if not query or not query.strip():
            return [], 0.0

        excluded = exclude_fact_ids or set()
        fts_expr = tokenize_query_for_fts(query)
        fts_hits = await self.backend.fts_search(fts_expr, limit=40) if fts_expr else []

        categories = {c.category_key: c for c in await self.backend.list_categories()}
        recall_chitchat = is_chitchat_recall_query(query)

        now = time.time()
        candidate_meta: Dict[Tuple[str, str], RetrievedMemory] = {}
        fts_ranks: Dict[Tuple[str, str], int] = {}

        for rank_idx, (item_id, item_type, fts_norm) in enumerate(fts_hits, start=1):
            if item_type == "fact" and item_id in excluded:
                continue
            mem = await self._load_active_item(
                item_id,
                item_type,
                fts_norm=fts_norm,
                now=now,
                categories=categories,
                recall_chitchat=recall_chitchat,
            )
            if mem is None:
                continue
            key = (item_type, item_id)
            candidate_meta[key] = mem
            fts_ranks[key] = rank_idx

        # Optional vector hybrid path with strict timeout fallback to FTS5
        vector_ranks: Dict[Tuple[str, str], int] = {}
        if self.embedding_provider.enabled and self.embedding_provider.model_name:
            try:
                vecs = await asyncio.wait_for(
                    self.embedding_provider.embed_texts([query]),
                    timeout=max(0.05, self.timeout_ms / 1000.0),
                )
                query_vec = vecs[0] if vecs else []
                if query_vec:
                    embedded = await self.backend.list_embedded_items(
                        self.embedding_provider.model_name
                    )
                    scored_vecs: List[Tuple[str, str, float]] = []
                    for item_id, item_type, blob in embedded:
                        if item_type == "fact" and item_id in excluded:
                            continue
                        sim = cosine_similarity(query_vec, unpack_embedding(blob))
                        if sim > 0.1:
                            scored_vecs.append((item_id, item_type, sim))
                    scored_vecs.sort(key=lambda x: x[2], reverse=True)
                    for rank_idx, (item_id, item_type, _sim) in enumerate(
                        scored_vecs[:30], start=1
                    ):
                        key = (item_type, item_id)
                        vector_ranks[key] = rank_idx
                        if key not in candidate_meta:
                            mem = await self._load_active_item(
                                item_id,
                                item_type,
                                fts_norm=0.0,
                                now=now,
                                categories=categories,
                                recall_chitchat=recall_chitchat,
                            )
                            if mem is not None:
                                candidate_meta[key] = mem
            except asyncio.TimeoutError:
                logger.warning(
                    "Embedding query timed out (>%dms); silently falling back to pure FTS5.",
                    self.timeout_ms,
                )
            except Exception as exc:
                logger.warning("Embedding query failed (%s); falling back to pure FTS5.", exc)

        results: List[RetrievedMemory] = []
        if vector_ranks:
            for key, mem in candidate_meta.items():
                rrf_score = 0.0
                if key in fts_ranks:
                    rrf_score += 1.0 / (_RRF_K + fts_ranks[key])
                if key in vector_ranks:
                    rrf_score += 1.0 / (_RRF_K + vector_ranks[key])
                mem.score = rrf_score + 0.002 * mem.score
                results.append(mem)
        else:
            results = list(candidate_meta.values())

        results.sort(key=lambda r: r.score, reverse=True)
        if not recall_chitchat:
            # Cap raw hot message hits on normal queries so they cannot crowd out curated facts/episodes
            selected: List[RetrievedMemory] = []
            msg_count = 0
            max_message_slots = 1
            for item in results:
                if item.item_type == "message":
                    if msg_count >= max_message_slots:
                        continue
                    msg_count += 1
                selected.append(item)
                if len(selected) >= top_k:
                    break
            final_results = selected
        else:
            final_results = results[:top_k]

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return final_results, elapsed_ms

    def _category_params(
        self,
        category_key: str,
        categories: Dict[str, MemoryCategory],
        *,
        recall_chitchat: bool,
    ) -> Tuple[float, float]:
        cat = categories.get(category_key)
        weight = cat.retrieval_weight if cat else 0.85
        decay = cat.decay_rate if cat else 0.05
        if category_key == "chitchat" and recall_chitchat:
            weight = max(weight, 1.05)
        return weight, decay

    async def _load_active_item(
        self,
        item_id: str,
        item_type: str,
        *,
        fts_norm: float,
        now: float,
        categories: Dict[str, MemoryCategory],
        recall_chitchat: bool,
    ) -> Optional[RetrievedMemory]:
        if item_type == "fact":
            fact = await self.backend.get_fact(item_id)
            if fact is None or fact.status != "active":
                return None
            weight, decay = self._category_params(
                fact.category, categories, recall_chitchat=recall_chitchat
            )
            days_ago = max(0.0, (now - fact.updated_at) / 86400.0)
            recency = math.exp(-decay * days_ago)
            base_score = 0.55 * fts_norm + 0.25 * recency + 0.20 * fact.importance
            return RetrievedMemory(
                item_id=fact.fact_id,
                item_type="fact",
                content=fact.content,
                category=fact.category,
                importance=fact.importance,
                score=base_score * weight,
                updated_at=fact.updated_at,
            )

        if item_type == "episode":
            ep = await self.backend.get_episode(item_id)
            if ep is None or ep.status != "active":
                return None
            cat_key = "chitchat" if ep.kind == "chitchat" else "work_log"
            weight, decay = self._category_params(
                cat_key, categories, recall_chitchat=recall_chitchat
            )
            days_ago = max(0.0, (now - ep.time_end) / 86400.0)
            recency = math.exp(-decay * days_ago)
            base_score = 0.55 * fts_norm + 0.25 * recency + 0.20 * ep.importance
            return RetrievedMemory(
                item_id=ep.episode_id,
                item_type="episode",
                content=ep.summary,
                category=cat_key,
                importance=ep.importance,
                score=base_score * weight,
                updated_at=ep.time_end,
            )

        if item_type == "message":
            msg = await self.backend.get_message(int(item_id))
            if msg is None:
                return None
            weight, decay = self._category_params(
                "chitchat", categories, recall_chitchat=recall_chitchat
            )
            days_ago = max(0.0, (now - msg.created_at) / 86400.0)
            recency = math.exp(-decay * days_ago)
            # Keep hot message raw hits slightly below curated facts/episodes unless recalled explicitly
            msg_boost = 0.95 if recall_chitchat else 0.75
            base_score = 0.60 * fts_norm + 0.25 * recency + 0.15 * 0.5
            return RetrievedMemory(
                item_id=str(msg.msg_id),
                item_type="message",
                content=f"[历史对话/{msg.role}] {msg.content}",
                category="chitchat",
                importance=0.5,
                score=base_score * weight * msg_boost,
                updated_at=msg.created_at,
            )

        return None
