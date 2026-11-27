from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

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


def _score_embeddings(
    query_vec: List[float],
    embedded: List[Tuple[str, str, bytes]],
    excluded: Set[str],
) -> List[Tuple[str, str, float]]:
    """Pure-Python vector scan; runs in a worker thread, not the event loop."""
    scored: List[Tuple[str, str, float]] = []
    for item_id, item_type, blob in embedded:
        if item_type == "fact" and item_id in excluded:
            continue
        sim = cosine_similarity(query_vec, unpack_embedding(blob))
        if sim > 0.1:
            scored.append((item_id, item_type, sim))
    scored.sort(key=lambda x: x[2], reverse=True)
    return scored


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

        # One bulk fetch per type instead of up to 40 sequential get_* queries.
        fts_keys = [(str(item_type), str(item_id)) for item_id, item_type, _ in fts_hits]
        fts_records = await self._prefetch_records(fts_keys, excluded)
        for rank_idx, (item_id, item_type, fts_norm) in enumerate(fts_hits, start=1):
            key = (item_type, item_id)
            if item_type == "fact" and item_id in excluded:
                continue
            mem = self._score_active_item(
                key,
                fts_records.get(key),
                fts_norm=fts_norm,
                now=now,
                categories=categories,
                recall_chitchat=recall_chitchat,
            )
            if mem is None:
                continue
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
                    scored_vecs = await asyncio.to_thread(
                        _score_embeddings, query_vec, embedded, excluded
                    )
                    missing_keys = [
                        (str(item_type), str(item_id))
                        for item_id, item_type, _sim in scored_vecs[:30]
                        if (item_type, item_id) not in candidate_meta
                    ]
                    extra_records = await self._prefetch_records(
                        missing_keys, excluded
                    )
                    for rank_idx, (item_id, item_type, _sim) in enumerate(
                        scored_vecs[:30], start=1
                    ):
                        key = (item_type, item_id)
                        vector_ranks[key] = rank_idx
                        if key not in candidate_meta:
                            mem = self._score_active_item(
                                key,
                                extra_records.get(key) or fts_records.get(key),
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

    async def _bulk_records(
        self, bulk_name: str, fallback_name: str, ids: List[Any]
    ) -> Dict[Any, Any]:
        if not ids:
            return {}
        bulk = getattr(self.backend, bulk_name, None)
        if callable(bulk):
            return await bulk(ids)
        out: Dict[Any, Any] = {}
        for item_id in ids:
            record = await getattr(self.backend, fallback_name)(item_id)
            if record is not None:
                out[item_id] = record
        return out

    async def _prefetch_records(
        self,
        keys: List[Tuple[str, str]],
        excluded: Set[str],
    ) -> Dict[Tuple[str, str], Any]:
        fact_ids: List[str] = []
        episode_ids: List[str] = []
        message_ids: List[int] = []
        for item_type, item_id in keys:
            if item_type == "fact":
                if item_id in excluded:
                    continue
                fact_ids.append(item_id)
            elif item_type == "episode":
                episode_ids.append(item_id)
            elif item_type == "message":
                try:
                    message_ids.append(int(item_id))
                except (TypeError, ValueError):
                    continue

        facts = await self._bulk_records("get_facts_by_ids", "get_fact", fact_ids)
        episodes = await self._bulk_records(
            "get_episodes_by_ids", "get_episode", episode_ids
        )
        messages = await self._bulk_records(
            "get_messages_by_ids", "get_message", message_ids
        )

        out: Dict[Tuple[str, str], Any] = {}
        for fact_id, record in facts.items():
            out[("fact", str(fact_id))] = record
        for episode_id, record in episodes.items():
            out[("episode", str(episode_id))] = record
        for msg_id, record in messages.items():
            out[("message", str(msg_id))] = record
        return out

    def _score_active_item(
        self,
        key: Tuple[str, str],
        record: Any,
        *,
        fts_norm: float,
        now: float,
        categories: Dict[str, MemoryCategory],
        recall_chitchat: bool,
    ) -> Optional[RetrievedMemory]:
        item_type, item_id = key
        if record is None:
            return None
        if item_type == "fact":
            fact = record
            if fact.status != "active":
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
            ep = record
            if ep.status != "active":
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
            msg = record
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
