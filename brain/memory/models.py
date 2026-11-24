from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal, Optional

FactStatus = Literal["active", "superseded", "deleted"]
EpisodeKind = Literal["conversation", "desktop_footprint"]
EpisodeStatus = Literal["active", "deleted"]
SessionStatus = Literal["active", "archived"]
MutationOp = Literal["ADD", "UPDATE", "DELETE", "NONE"]

_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_.\-]+")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


def new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:12]}"


def new_fact_id() -> str:
    return f"fact_{uuid.uuid4().hex[:12]}"


def new_episode_id() -> str:
    return f"ep_{uuid.uuid4().hex[:12]}"


def tokenize_for_fts(text: str) -> str:
    """Zero-dependency bilingual tokenizer producing FTS5-friendly ASCII words + CJK bigrams."""
    if not text:
        return ""
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        if tok and tok not in seen:
            seen.add(tok)
            tokens.append(tok)

    for m in _ASCII_WORD_RE.finditer(text):
        word = m.group(0).lower().strip("._-")
        if word:
            _add(word)
            for sub in re.split(r"[._\-]+", word):
                if sub:
                    _add(sub)

    for m in _CJK_RUN_RE.finditer(text):
        run = m.group(0)
        n = len(run)
        if n == 1:
            _add(run)
        else:
            for i in range(n - 1):
                _add(run[i : i + 2])
            for ch in run:
                _add(ch)

    return " ".join(tokens)


def tokenize_query_for_fts(query: str) -> str:
    """Tokenize a search query into FTS5 MATCH expression (OR across bigrams & ASCII tokens)."""
    if not query:
        return ""
    # Strip interrogative/pronoun noise words for natural-language recall queries
    cleaned_query = query
    for noise in ("之前", "刚才", "刚刚", "主要", "正在", "什么", "哪些", "怎么", "请问", "一下", "现在", "我的", "我在"):
        cleaned_query = cleaned_query.replace(noise, " ")

    parts: list[str] = []
    seen: set[str] = set()

    def _add_term(term: str) -> None:
        clean = term.replace('"', "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            parts.append(f'"{clean}"')

    for m in _ASCII_WORD_RE.finditer(cleaned_query):
        w = m.group(0).lower().strip("._-")
        if w:
            _add_term(w)

    for m in _CJK_RUN_RE.finditer(cleaned_query):
        run = m.group(0)
        if len(run) == 1:
            _add_term(run)
        else:
            for i in range(len(run) - 1):
                _add_term(run[i : i + 2])
            if len(run) <= 4:
                for ch in run:
                    _add_term(ch)

    # Expand activity-recap queries so desktop_footprint episodes are always eligible
    if any(k in query for k in ("忙什么", "干嘛", "足迹", "工作", "活动", "时间线")):
        _add_term("桌面")
        _add_term("工作")
        _add_term("足迹")

    return " OR ".join(parts)


@dataclass
class SessionRecord:
    session_id: str
    started_at: float
    updated_at: float
    rolling_summary: str = ""
    last_summarized_msg_id: int = 0
    status: SessionStatus = "active"


@dataclass
class MessageRecord:
    msg_id: int
    session_id: str
    turn_id: str
    role: str
    content: str
    emotion: str = "neutral"
    desktop_context_summary: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class MemoryFact:
    fact_id: str
    category: str
    content: str
    importance: float = 0.7
    confidence: float = 0.9
    status: FactStatus = "active"
    source_session_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_accessed_at: float = field(default_factory=time.time)
    embedding: Optional[bytes] = None
    embedding_model: Optional[str] = None


@dataclass
class MemoryEpisode:
    episode_id: str
    kind: EpisodeKind
    summary: str
    keywords: str = ""
    importance: float = 0.6
    time_start: float = field(default_factory=time.time)
    time_end: float = field(default_factory=time.time)
    status: EpisodeStatus = "active"
    created_at: float = field(default_factory=time.time)
    embedding: Optional[bytes] = None
    embedding_model: Optional[str] = None


@dataclass
class MemoryMutation:
    op: MutationOp
    category: str = "fact"
    content: str = ""
    importance: float = 0.7
    confidence: float = 0.9
    target_fact_id: Optional[str] = None


@dataclass
class RetrievedMemory:
    item_id: str
    item_type: Literal["fact", "episode"]
    content: str
    category: str
    importance: float
    score: float
    updated_at: float
