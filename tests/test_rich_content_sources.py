from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List, Optional

import pytest

from brain.gemini_native import grounding_sources
from brain.rich_content import (
    clean_answer_and_sources,
    extract_source_snippets,
    is_generic_source_title,
    label_sources_with_llm,
    needs_source_labeling,
)


class _FakeTaggingRouter:
    def __init__(self, response_text: str, *, delay: float = 0.0, fail: bool = False) -> None:
        self.response_text = response_text
        self.delay = delay
        self.fail = fail
        self.calls: int = 0
        self.last_json_mode: Optional[bool] = None
        self.last_profile: Optional[str] = None

    async def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        profile_name: Optional[str] = None,
        *,
        json_mode: bool = False,
    ) -> AsyncIterator[str]:
        self.calls += 1
        self.last_json_mode = json_mode
        self.last_profile = profile_name
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom")
        yield self.response_text


def test_grounding_sources_extracts_snippets_from_supports() -> None:
    metadata = {
        "groundingChunks": [
            {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/aaa", "title": "youtube.com"}},
            {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/bbb", "title": "youtube.com"}},
        ],
        "groundingSupports": [
            {
                "segment": {"text": "OWCS 第三阶段韩国区决赛 T1 对 Crazy Raccoon 全场录像"},
                "groundingChunkIndices": [0],
            },
            {
                "segment": {"text": "暴雪嘉年华 守望先锋世界杯 中国队高光集锦"},
                "groundingChunkIndices": [1],
            },
        ],
    }
    res = grounding_sources(metadata)
    assert len(res) == 2
    assert "T1 对 Crazy Raccoon" in res[0]["snippet"]
    assert "守望先锋世界杯" in res[1]["snippet"]


def test_needs_source_labeling_detects_bare_domains_and_duplicates() -> None:
    assert is_generic_source_title("youtube.com", "youtube.com")
    assert is_generic_source_title("https://youtu.be/abc", "youtu.be")
    assert not is_generic_source_title("T1 vs CR 决赛录像", "youtube.com")

    dup_sources = [
        {"title": "youtube.com", "url": "https://example.com/1", "domain": "youtube.com"},
        {"title": "youtube.com", "url": "https://example.com/2", "domain": "youtube.com"},
    ]
    assert needs_source_labeling(dup_sources)

    same_title = [
        {"title": "守望先锋决赛", "url": "https://example.com/1", "domain": "youtube.com"},
        {"title": "守望先锋决赛", "url": "https://example.com/2", "domain": "youtube.com"},
    ]
    assert needs_source_labeling(same_title)

    distinct = [
        {"title": "T1 vs CR 录像", "url": "https://example.com/1", "domain": "youtube.com"},
        {"title": "OWCS 总决赛高光", "url": "https://example.com/2", "domain": "youtube.com"},
    ]
    assert not needs_source_labeling(distinct)


@pytest.mark.asyncio
async def test_label_sources_with_llm_skips_when_already_distinct() -> None:
    router = _FakeTaggingRouter("{}")
    sources = [
        {"title": "中央气象台", "url": "https://www.weather.com.cn/", "domain": "weather.com.cn"},
        {"title": "Google 官方博客", "url": "https://blog.google/technology/ai/", "domain": "blog.google"},
    ]
    result = await label_sources_with_llm(sources, context_text="天气预报与博客", llm_router=router)
    assert [s["title"] for s in result] == ["中央气象台", "Google 官方博客"]
    assert router.calls == 0


@pytest.mark.asyncio
async def test_label_sources_with_llm_assigns_distinct_labels_for_youtube_chips() -> None:
    raw_sources = [
        {
            "type": "url_citation",
            "url_citation": {
                "url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/1",
                "title": "youtube.com",
                "snippet": "暴雪嘉年华 守望先锋世界杯 中国队全场回放",
            },
        },
        {
            "type": "url_citation",
            "url_citation": {
                "url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/2",
                "title": "youtube.com",
                "snippet": "OWCS 第三阶段韩国区决赛 T1 对 Crazy Raccoon",
            },
        },
        {
            "type": "url_citation",
            "url_citation": {
                "url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/3",
                "title": "youtube.com",
                "snippet": "Super 二路陪看解说精华",
            },
        },
    ]
    cleaned_text, base = clean_answer_and_sources("帮你找了几场热门 OW 比赛", raw_sources)
    assert cleaned_text == "帮你找了几场热门 OW 比赛"
    snippets = extract_source_snippets(raw_sources)
    assert len(snippets) == 3

    fake_json = json.dumps(
        {
            "labels": [
                {"index": 0, "label": "暴雪嘉年华世界杯"},
                {"index": 1, "label": "T1 vs CR 韩国决赛"},
                {"index": 2, "label": "Super 二路陪看"},
            ]
        },
        ensure_ascii=False,
    )
    router = _FakeTaggingRouter(f"```json\n{fake_json}\n```")
    labeled = await label_sources_with_llm(
        base,
        context_text="1. 暴雪嘉年华世界杯 2. OWCS 第三阶段 T1 对 CR 3. 主播二路陪看",
        snippets_by_url=snippets,
        llm_router=router,
        profile_name="slot_fast_utility",
    )
    assert router.calls == 1
    assert router.last_json_mode is True
    assert router.last_profile == "slot_fast_utility"
    assert [s["title"] for s in labeled] == [
        "暴雪嘉年华世界杯",
        "T1 vs CR 韩国决赛",
        "Super 二路陪看",
    ]
    assert all(set(s.keys()) == {"title", "url", "domain"} for s in labeled)


@pytest.mark.asyncio
async def test_label_sources_with_llm_timeout_falls_back_to_unique_labels() -> None:
    sources = [
        {"title": "youtube.com", "url": "https://example.com/a", "domain": "youtube.com"},
        {"title": "youtube.com", "url": "https://example.com/b", "domain": "youtube.com"},
    ]
    router = _FakeTaggingRouter("{}", delay=0.2)
    labeled = await label_sources_with_llm(
        sources,
        context_text="无明显差异正文",
        llm_router=router,
        timeout_sec=0.05,
    )
    assert [s["title"] for s in labeled] == ["youtube.com · 1", "youtube.com · 2"]
